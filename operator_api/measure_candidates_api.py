"""API glue for candidate business measure definitions: per-reader masking, exclusion
enforcement, and automatic capture from accepted Ask answers and saved Insights tiles."""
from __future__ import annotations

import logging
from typing import Any, Mapping, Optional, Sequence

from operating import measure_candidates as mc
from operating import measures as ms
from operating.ontology_tools_identity import ToolError

from . import feedback as fb
from . import models as m

log = logging.getLogger(__name__)


def _governed_signatures() -> frozenset[str]:
    """Canonical SQL signatures of every currently governed measure's default statement."""
    sigs: set[str] = set()
    for mid, entry in ms.measures().items():
        try:
            if entry.get("sql"):
                sigs.add(mc.statement_signature(entry["sql"]))
            else:
                sql, _ = ms.statement(mid, unchecked=True)
                sigs.add(mc.statement_signature(sql))
        except (ToolError, ValueError, KeyError):
            continue
    return frozenset(sigs)


def is_already_governed(sql: str) -> bool:
    """Whether ``sql`` already matches a governed measure's statement."""
    try:
        sig = mc.statement_signature(sql)
    except ToolError:
        return False
    return sig in _governed_signatures()


def _proposer_info(reader: Any, guard: Any) -> tuple[str, Optional[str], bool]:
    hidden = fb.reader_excluded(reader, guard)
    name = None if hidden else reader.display_name
    return reader.key, name, hidden


def capture_from_accepted_answer(
    store: mc.CandidateStore,
    backend: Any,
    reader: Any,
    answer_id: str,
    markings: Optional[Sequence[str]],
) -> list[dict[str, Any]]:
    """Capture ad-hoc / derived SQL from a thumbs-up Ask answer into ``store``."""
    try:
        kept = backend.answer_context(reader, answer_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not read answer context for candidate capture: %s", type(exc).__name__)
        return []
    queries = kept.get("queries") or []
    if not isinstance(queries, list) or not queries:
        return []
    guard = fb.exclusion()
    proposer_key, proposer_name, hidden = _proposer_info(reader, guard)
    question = str(kept.get("question") or "")
    captured: list[dict[str, Any]] = []
    seen_sigs: set[str] = set()
    for q in queries:
        if not isinstance(q, Mapping):
            continue
        if q.get("measure_id"):
            continue
        sql = q.get("sql")
        if not isinstance(sql, str) or not sql.strip():
            continue
        if is_already_governed(sql):
            continue
        try:
            sig = mc.statement_signature(sql)
        except ToolError:
            continue
        if sig in seen_sigs:
            continue
        seen_sigs.add(sig)
        try:
            cand = store.capture(
                sql=sql,
                source_kind="accepted_answer",
                title=str(q.get("title") or ""),
                question=question,
                source_ref=answer_id,
                proposer_key=proposer_key,
                proposer_name=proposer_name,
                proposer_hidden=hidden,
                markings=markings,
                guard=guard,
            )
            captured.append(cand)
        except mc.CandidateError as exc:
            log.info("skipped answer candidate capture (%s): %s", exc.code, exc.message)
    return captured


def capture_from_saved_tile(
    store: mc.CandidateStore,
    backend: Any,
    reader: Any,
    *,
    tile_id: str,
    title: str,
    question: str,
    sql: str,
) -> Optional[dict[str, Any]]:
    """Capture ad-hoc SQL from a saved/pinned Insights tile into ``store``."""
    if not isinstance(sql, str) or not sql.strip() or is_already_governed(sql):
        return None
    guard = fb.exclusion()
    proposer_key, proposer_name, hidden = _proposer_info(reader, guard)
    markings = fb.served_markings(backend, reader)
    try:
        return store.capture(
            sql=sql,
            source_kind="saved_tile",
            title=title,
            question=question,
            source_ref=tile_id,
            proposer_key=proposer_key,
            proposer_name=proposer_name,
            proposer_hidden=hidden,
            markings=markings,
            guard=guard,
        )
    except mc.CandidateError as exc:
        log.info("skipped saved tile candidate capture (%s): %s", exc.code, exc.message)
        return None


def _writer_excluded(guard: Any, key: Any) -> bool:
    if guard is None or getattr(guard, "unavailable", None):
        return True
    for value in str(key or "").lower().split(","):
        tail = value.rsplit(":", 1)[-1]
        if (mc._OID_RE.fullmatch(tail) and mc._oid_excluded(guard, tail)) or (
            mc._MAIL_RE.fullmatch(tail) and mc._mail_excluded(guard, tail)
        ):
            return True
    return False


def present_candidates(
    rows: Sequence[Mapping[str, Any]],
    *,
    backend: Any,
    reader: Any,
    owner: bool,
    admin: bool,
    guard: Any = None,
) -> list[m.MeasureCandidate]:
    """Filter and mask candidates for ``reader`` according to people exclusion and markings."""
    active_guard = fb.exclusion() if guard is None else guard
    held: Optional[frozenset[str]] = None
    needs_grant = (not owner) and any(
        r.get("proposer_key") != reader.key and r.get("status") != "approved"
        for r in rows
    )
    if needs_grant or (not owner and any(r.get("proposer_key") != reader.key for r in rows)):
        held, _ = fb._grant(backend, reader, [])

    out: list[m.MeasureCandidate] = []
    for raw in rows:
        mine = raw.get("proposer_key") == reader.key
        approved = raw.get("status") == "approved"
        # Regular staff only see their own proposals and approved shared definitions.
        if not owner and not admin and not mine and not approved:
            continue

        cleaned = mc.reclean_candidate(raw, active_guard)
        if not mine and (raw.get("proposer_hidden") or _writer_excluded(active_guard, raw.get("proposer_key"))):
            cleaned["proposer_name"] = fb.COLLEAGUE

        withheld: set[str] = set()
        if not owner and not mine:
            reader_held = held or frozenset()
            carried = raw.get("markings")
            lacking = set(fb.SENSITIVE if carried is None else carried) - reader_held
            if lacking and not approved:
                cleaned["sql"] = None
                cleaned["question"] = None
                cleaned["tile_title"] = None
                cleaned["source_ref"] = None
                cleaned["proposed_id"] = "withheld"
                cleaned["proposed_name"] = "Withheld candidate definition"
                cleaned["proposed_description"] = None
                cleaned["proposed_view"] = ""
                cleaned["proposed_value"] = ""
                cleaned["proposed_per"] = ""
                cleaned["proposed_date"] = ""
                cleaned["proposed_where"] = ""
                cleaned["proposed_dimensions"] = []
                cleaned["views"] = []
                withheld |= lacking
            personal_lacking = {"finance", "pii"} - reader_held
            if personal_lacking:
                for field in (
                    "question",
                    "tile_title",
                    "sql",
                    "source_ref",
                    "proposed_name",
                    "proposed_description",
                    "proposed_where",
                ):
                    if fb._personal(cleaned.get(field)):
                        if field == "proposed_name":
                            cleaned["proposed_name"] = "Withheld candidate definition"
                            cleaned["proposed_id"] = "withheld"
                        elif field == "proposed_where":
                            cleaned["proposed_where"] = ""
                        else:
                            cleaned[field] = None
                        if field == "sql":
                            cleaned["proposed_where"] = ""
                        withheld |= personal_lacking

        out.append(
            m.MeasureCandidate(
                id=str(cleaned["id"]),
                signature=str(cleaned["signature"]),
                status=cleaned["status"],
                source_kind=cleaned["source_kind"],
                source_ref=cleaned.get("source_ref"),
                tile_title=cleaned.get("tile_title"),
                question=cleaned.get("question"),
                sql=cleaned.get("sql"),
                views=list(cleaned.get("views") or []),
                sql_mode=cleaned.get("sql_mode") or "custom_sql",
                usage_count=int(cleaned.get("usage_count") or 1),
                first_seen_at=str(cleaned["first_seen_at"]),
                last_seen_at=str(cleaned["last_seen_at"]),
                proposed_id=str(cleaned.get("proposed_id") or "withheld"),
                proposed_name=str(cleaned.get("proposed_name") or "Withheld candidate definition"),
                proposed_description=cleaned.get("proposed_description"),
                proposed_owner=str(cleaned.get("proposed_owner") or "Company"),
                proposed_unit=str(cleaned.get("proposed_unit") or "number"),
                proposed_grain=str(cleaned.get("proposed_grain") or "row"),
                proposed_view=str(cleaned.get("proposed_view") or ""),
                proposed_value=str(cleaned.get("proposed_value") or ""),
                proposed_per=str(cleaned.get("proposed_per") or ""),
                proposed_date=str(cleaned.get("proposed_date") or ""),
                proposed_where=str(cleaned.get("proposed_where") or ""),
                proposed_dimensions=list(cleaned.get("proposed_dimensions") or []),
                proposer_name=cleaned.get("proposer_name"),
                reviewed_by=cleaned.get("reviewed_by"),
                reviewed_at=cleaned.get("reviewed_at"),
                dismiss_reason=cleaned.get("dismiss_reason"),
                approved_measure_id=cleaned.get("approved_measure_id"),
                withheld=sorted(withheld),
            )
        )
    return out
