"""Emailed brief or answer with its chart (Genie item 11, email only): drafts an email
card for the signed-in reader, optionally attaching an Excel/Word/PowerPoint/PDF copy of
the chart or brief, and routes it through the one confirm-before-send card-actions desk
(``operating.card_actions.ActionDesk`` -> ``integrations.graph_send.ActionSender``).
Live send stays gated by ``PLX_ACTIONS_LIVE=1`` and ``PLX_ACTIONS_SENDERS`` (Vince-only on
the host). Teams delivery is never offered or accepted on these cards."""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import threading
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional, Sequence

from operating import card_actions as ca
from operating import instruct_files as files
from operating.card_text import mask_served
from operating.doc_render import without_doubt_marks
from operating.file_modes import keep_private

from . import charts as ch
from . import models as m
from .auth import Reader
from .errors import UserFacing

LOG = logging.getLogger(__name__)
BRIEF_CARD = "brief:"
_ADDRESS = re.compile(r"\b[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")


def _strings_in(value: Any) -> Iterator[str]:
    """Yield every string nested inside ``value``."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for v in value.values():
            yield from _strings_in(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings_in(v)


def _addresses_in(*values: Any) -> list[str]:
    """Return sorted unique lowercase email addresses across all strings in ``values``."""
    found: set[str] = set()
    for val in values:
        for text in _strings_in(val):
            for addr in _ADDRESS.findall(text):
                found.add(addr.lower())
    return sorted(found)


def _validate_chart_types(chart: Mapping[str, Any]) -> None:
    """Reject non-list sequence fields or non-object entries before shape validation."""
    for key in ("labels", "series", "panels", "xs"):
        val = chart.get(key)
        if val is not None and not isinstance(val, (list, tuple)):
            raise ValueError(f"Chart '{key}' must be a list.")
    for s in chart.get("series") or ():
        if not isinstance(s, Mapping):
            raise TypeError("Chart series entries must be objects.")
        vals = s.get("values")
        if vals is not None and not isinstance(vals, (list, tuple)):
            raise ValueError("Chart series 'values' must be a list.")
    for p in chart.get("panels") or ():
        if not isinstance(p, Mapping):
            raise TypeError("Chart panels must be objects.")
        _validate_chart_types(p)


class _DraftStore:
    """Per-reader kept email-draft cards (0600 JSON file under state_dir, or in-memory)."""

    def __init__(self, path: Optional[Path]) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._mem: dict[str, dict[str, Any]] = {}

    @classmethod
    def for_state_dir(cls, state_dir: Any) -> "_DraftStore":
        return cls(Path(state_dir) / "email-drafts" / "drafts.json" if state_dir else None)

    def _read(self) -> dict[str, dict[str, Any]]:
        if self.path is None:
            return dict(self._mem)
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            LOG.exception("Failed to read email drafts store at %s", self.path)
            raise
        if not isinstance(raw, Mapping):
            LOG.error("Corrupt email drafts store at %s: expected JSON object", self.path)
            raise ValueError(f"Corrupt email drafts store at {self.path}: expected JSON object")
        return dict(raw)

    def _write(self, data: Mapping[str, Any]) -> None:
        if self.path is None:
            self._mem = dict(data)
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        keep_private(self.path.parent, 0o700)
        tmp = self.path.with_name(f".{self.path.name}.{secrets.token_hex(4)}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        keep_private(tmp, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        os.replace(tmp, self.path)

    def put(self, reader_key: str, card_id: str, entry: Mapping[str, Any]) -> None:
        with self._lock:
            data = self._read()
            data[card_id] = {**entry, "reader": reader_key}
            self._write(data)

    def get(self, reader_key: str, card_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._read().get(card_id)
        if not isinstance(row, Mapping) or row.get("reader") != reader_key:
            return None
        return dict(row)


class BriefEmailMixin:
    """Needs ``_uploads()``, ``_instruct_guard()``, ``_mailbox(reader)`` and ``state_dir``."""

    _brief_draft_store: Optional[_DraftStore] = None
    _brief_draft_lock = threading.Lock()

    def _brief_drafts(self) -> _DraftStore:
        with self._brief_draft_lock:
            if self._brief_draft_store is None:
                self._brief_draft_store = _DraftStore.for_state_dir(
                    getattr(self, "state_dir", None))
            return self._brief_draft_store

    def prepare_email_draft(self, reader: Reader, body: m.EmailDraftRequest) -> m.EmailDraftPrepared:
        """Build an email draft card (optionally attaching the chart/brief workbook or doc)
        ready for confirm-before-send preview through ``/api/actions/preview``."""
        from .feedback import clean
        from .instruct_api import _clean_chart, _clean_strings

        if getattr(reader.session, "view_as", False):
            raise UserFacing("view_as_refused", "Nothing is drafted while you view as someone else.", 403)
        guard = self._instruct_guard()  # type: ignore[attr-defined]
        if guard is None:
            raise UserFacing("people_exclusion_unavailable",
                             "Email drafting is paused while the people-exclusion list is unavailable.", 503)

        def scrub(text: Any) -> str:
            raw = str(text or "").strip()
            if not raw:
                return ""
            cleaned = clean(raw, guard, max(len(raw), 1)) or ""
            return without_doubt_marks(mask_served(cleaned)).strip()

        title = scrub(body.title) or "Workbench brief"
        summary = scrub(body.summary)
        charts: list[dict[str, Any]] = []
        for raw_chart in body.charts or ():
            if not isinstance(raw_chart, Mapping):
                raise UserFacing("invalid_request", "Each chart must be a JSON object.", 422)
            try:
                _validate_chart_types(raw_chart)
                ch._check_shape(raw_chart)
                cleaned = _clean_chart(dict(raw_chart), scrub)
                ch._check_shape(cleaned)
            except (ch.NotDrawn, ValueError, KeyError, TypeError, AttributeError) as exc:
                raise UserFacing("invalid_request", f"The chart could not be built: {exc}", 422) from None
            charts.append(cleaned)
        citations = [
            _clean_strings(dict(c), scrub)
            for c in (body.citations or ())
            if isinstance(c, Mapping) and (c.get("sql") or c.get("measure"))
        ]

        if not summary and not charts:
            raise UserFacing("invalid_request", "Nothing to email: provide a summary or a chart.", 422)

        upload_id: Optional[str] = None
        attachment_name: Optional[str] = None
        if charts or body.attach_file:
            fmt = body.format or ("xlsx" if charts else "docx")
            try:
                for chart in charts:
                    ch._check_shape(chart)
                if fmt == "xlsx" and len(charts) == 1 and not summary and not citations:
                    data = ch.as_file(charts[0], "xlsx")
                else:
                    doc_body = f"# {title}\n\n{summary}" if summary else f"# {title}"
                    if citations:
                        cite_lines = [
                            f"- {c.get('label') or 'Source'}: {c.get('sql') or ''}".strip()
                            for c in citations
                        ]
                        doc_body += "\n\n## Sources and statements\n" + "\n".join(cite_lines)
                    data = files.make(fmt, doc_body, charts=charts)
            except (ch.NotDrawn, files.FileRefused, ValueError, KeyError, TypeError) as exc:
                raise UserFacing("invalid_request", f"The attachment could not be built: {exc}", 422) from None
            if len(data) > ca.MAX_ATTACHMENT_BYTES:
                raise UserFacing("attachment_too_large",
                                 "The attached chart or brief is larger than 3 MB and cannot be emailed.", 422)
            att_name = files.file_name(title, fmt)
            att_people = _addresses_in(title, summary, citations, charts)
            row = self._uploads().prepare(  # type: ignore[attr-defined]
                reader.key, name=att_name, kind=fmt, data=data, target=None, people=att_people)
            upload_id = row["upload_id"]
            attachment_name = att_name

        # Build the plain-text email body (summary + chart source notes + attachment line)
        parts: list[str] = []
        if summary:
            parts.append(summary)
        if charts:
            from operating import chart_ooxml as ox
            for chart in charts:
                try:
                    note_lines = [ln for ln in ox.provenance(chart) if not ln.startswith("Statement: ")]
                except (ValueError, KeyError, TypeError) as exc:
                    raise UserFacing("invalid_request", f"The chart could not be built: {exc}", 422) from None
                ctitle = str(chart.get("title") or "Chart")
                parts.append(f"{ctitle}: " + " ".join(note_lines))
        if attachment_name:
            parts.append(f"Attached: {attachment_name} (includes data and source statements).")
        email_body = "\n\n".join(parts)
        if len(email_body) > ca.MAX_BODY:
            raise UserFacing("invalid_request",
                             f"Email body is longer than {ca.MAX_BODY:,} characters; attach as a file instead.", 422)

        subject = title if len(title) <= ca.MAX_SUBJECT else title.rsplit(" ", 1)[0]
        if len(subject) > ca.MAX_SUBJECT:
            subject = "Workbench brief"
        caller_to = bool(body.to and "@" in str(body.to))
        to_addr = (str(body.to).strip().lower() if caller_to
                   else self._mailbox(reader))  # type: ignore[attr-defined]
        card_id = BRIEF_CARD + secrets.token_hex(10)
        people = _addresses_in(subject, email_body, title, summary, citations, charts)
        self._brief_drafts().put(reader.key, card_id, {
            "subject": subject, "body": email_body, "to": to_addr,
            "to_proposed": caller_to,
            "upload_id": upload_id, "attachment_name": attachment_name, "people": people,
        })
        return m.EmailDraftPrepared(
            card_id=card_id, subject=subject, body=email_body, to=to_addr,
            attachment_name=attachment_name, upload_id=upload_id,
        )

    def brief_email_card(self, reader: Reader, card_id: str) -> Optional[dict[str, Any]]:
        """Rebuild a prepared brief/answer email card strictly for its reader, re-checking
        people exclusion on named addresses."""
        row = self._brief_drafts().get(reader.key, card_id)
        if row is None:
            return None
        from .feedback import _excluded_address
        guard = self._instruct_guard()  # type: ignore[attr-defined]
        named = row.get("people") or ()
        if named and (guard is None or any(_excluded_address(guard, str(a)) for a in named)):
            return None
        to = row.get("to")
        to_proposed = row["to_proposed"] if "to_proposed" in row else (to != self._mailbox(reader))  # type: ignore[attr-defined]
        if to:
            draft_to: Optional[dict[str, Any]] = (
                {"address": to, "source": None, "proposed": True}
                if to_proposed
                else {"address": to, "source": "reader"}
            )
        else:
            draft_to = None
        atts = [row["upload_id"]] if row.get("upload_id") else []
        return {
            "id": card_id,
            "headline": f"Email {row.get('subject') or 'brief'}",
            "subject": {"label": str(row.get("subject") or "Workbench brief")},
            "action": {"text": "Review recipients and confirm before sending."},
            "draft": {
                "channel": "email",
                "to": draft_to,
                "subject": row.get("subject") or "Workbench brief",
                "body": row.get("body") or "",
                "facts": [],
            },
            "attachments": atts,
            "teams_draft": None,
            "email_only": True,
            "playbook": True,
        }
