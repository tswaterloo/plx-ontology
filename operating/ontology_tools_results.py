"""What the ontology tools return: freshness and resolution gaps, cited values,
corroboration verdicts, cursors, filter checks, value and edge output, conflicts, the
retired host tools and the tool specs ``register`` publishes."""

from __future__ import annotations

import base64
import datetime as dt
import functools
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
import zlib
from collections.abc import Callable, Iterable, Mapping, MutableMapping, Sequence
from typing import TYPE_CHECKING, Any

from ontology.text_index import chunk_text
from ontology.types import Actor
from operating.injection_guard import is_unit
from operating.ontology_tools_identity import _compare_key, _gap, _Match, ToolError
from operating.ontology_tools_policy import (_citation, _DATE, _DIGEST, _envelope_bytes,
                                             _FIELD_PATH, _format_time, _HANDLE, _LINK_TYPE,
                                             _markings, _NAME, _OBJECT_ID, _parse_time,
                                             _TIER_RANK, _VERDICT_AGREES, COPIED,
                                             CORROBORATION_FEATURE, LINK_BELOW_FLOOR,
                                             LINK_TARGETS_LEAD, LINK_TARGETS_PARTIAL,
                                             load_contracts, MAX_GAPS, REFUSED_CODES,
                                             SCAN_CURSOR_PREFIX, TOOL_NAMES)

if TYPE_CHECKING:
    from operating.ontology_tools import OntologyTools


# --------------------------------------------------------------------------- pure helpers

def _pinned(arguments: Mapping[str, Any]) -> set[str]:
    """The properties a find's ``eq`` filters pin its query in (the filter value is the
    query text): its results must hold the query there."""
    query = arguments.get("query")
    if query is None:
        return set()
    return {f["property"] for f in arguments.get("filters", ())
            if f.get("op") == "eq" and f.get("value") == query}


def _names_identifier(query: Any) -> bool:
    """Whether a query can name a record's identifier: it holds a digit. Words alone (a
    business question) are still tried as every kind's whole value, but a carrier no
    source is configured for is then no answer the reader is missing."""
    return isinstance(query, str) and any(ch.isdigit() for ch in query)


def _resolution_gaps(resolution: Any, *, names_identifier: bool = True
                     ) -> list[dict[str, Any]]:
    """Typed gaps for carrier units a live resolution did not read in full. A unit
    the policy withheld is not named (it says nothing the reader may know); every
    other state is: what was not searched, so a no-match is never absence. A query that
    names no identifier (``_names_identifier``) drops only the carriers no source is
    configured for: the no-match still says absence is not established."""
    counted: dict[tuple[str, str, str, str], int] = {}
    for source, relation, path, state in resolution.unread:
        if state == "not_configured" and not names_identifier:
            continue
        if state != "withheld":
            counted[(source, relation, path, state)] = counted.get(
                (source, relation, path, state), 0) + 1
    out = []
    for (source, relation, path, state), count in sorted(counted.items()):
        gap_state = "not_ingested" if state in ("not_configured", "not_served", "unpinned") \
            else "stale" if state == "stale" else "unknown"
        # One code for a stale source, whether its projection entry or a carrier says so.
        reason = "stale_source" if state == "stale" else f"source_{state}"
        out.append(_gap(gap_state, reason, f"{source}:{relation}.{path}",
                        f"{count} live carrier read(s) {state.replace('_', ' ')}; "
                        f"{resolution.retried} read(s) retried once",
                        {"sources": [source]}))
    return out


@functools.lru_cache(maxsize=1)
def _freshness_label_pattern() -> re.Pattern[str]:
    """The contract's own pattern of ``$defs/freshness`` ``label``: its prefix and its
    length budget come from ``ontology_tool_contracts.json``, never a second constant."""
    return re.compile(load_contracts()["$defs"]["freshness"]["properties"]["label"]["pattern"])


def _capture_labels(labels: Sequence[str]) -> str | None:
    """``labels`` joined into one freshness label the contract accepts: as many whole
    labels as fit, then how many were left out (``; +N more``); a label is never cut.
    Every label stays on its unit in the entry's ``relations``. When not even one label
    and the count fit, the first whole label alone; None when no label fits at all."""
    fits = _freshness_label_pattern().search
    for shown in range(len(labels), 0, -1):
        left = len(labels) - shown
        text = "; ".join(labels[:shown]) + (f"; +{left} more" if left else "")
        if fits(text):
            return text
    return next((label for label in labels if fits(label)), None)


def _scope_freshness_entry(entry: Mapping[str, Any],
                           units: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """A source's contract freshness ``entry`` with how the units of the planned scope
    were served (``ontology.resolve.Resolution.scope_freshness``): a unit from a
    persisted capture or a held one whose refresh failed (``cached_mirror``) makes the
    source stale, served from that capture (``persisted_capture`` over
    ``cached_mirror``), labelled and as old as that capture; otherwise a unit read live makes it ``live``; a unit not served at
    all leaves its capture partial. Only the units not served live are listed."""
    out = dict(entry)
    copied = [dict(u) for _, u in sorted(units.items()) if u["served_from"] in COPIED]
    missing = [dict(u) for _, u in sorted(units.items()) if u["served_from"] == "none"]
    held = {u["relation"]: u for u in out.get("relations", ())}
    held.update({u["relation"]: u for u in copied + missing})
    if copied or out.get("served_from") in COPIED:
        served = [u["served_from"] for u in held.values() if u["served_from"] in COPIED]
        served.append(out.get("served_from") or "cached_mirror")
        out.update(stale=True, served_from="persisted_capture"
                   if "persisted_capture" in served else "cached_mirror")
        labels = sorted({u["label"] for u in held.values() if u.get("label")})
        label = _capture_labels(labels)
        if label:
            out["label"] = label
        moments = [(_parse_time(u["captured_at"]), u["captured_at"]) for u in copied
                   if u.get("captured_at")]
        if out.get("captured_at"):
            moments.append((_parse_time(out["captured_at"]), out["captured_at"]))
        moments = [m for m in moments if m[0] is not None]
        if moments:
            out["captured_at"] = min(moments)[1]
    elif any(u["served_from"] == "host_capture" for u in units.values()):
        # The host's own capture, within its bound: as old as its oldest unit read.
        mine = [u for u in units.values() if u["served_from"] == "host_capture"]
        moments = [(_parse_time(u["captured_at"]), u) for u in mine if u.get("captured_at")]
        moments = [m for m in moments if m[0] is not None]
        out["served_from"] = "host_capture"
        if moments and not out.get("stale"):  # a projection past its bound keeps its label
            oldest = min(moments, key=lambda m: m[0])[1]
            out["captured_at"] = oldest["captured_at"]
            if oldest.get("label"):
                out["label"] = oldest["label"]
    elif any(u["served_from"] == "live" for u in units.values()):
        out["served_from"] = "live"
    if missing and out.get("completeness") == "complete":
        out["completeness"] = "partial"
    if held:
        out["relations"] = [held[name] for name in sorted(held)][:32]
    return out


def _source_freshness_gaps(freshness: Sequence[Mapping[str, Any]],
                           elsewhere: Mapping[str, Sequence[str]] | None = None
                           ) -> list[dict[str, Any]]:
    """One gap per source whose capture is stale (a persisted capture: its label), whose
    state is unknown, one per source read only in part (``source_partly_read``), and one per source with units the live source did not serve at
    all and no persisted capture holds (``inaccessible``: their HTTP status and error
    code). A relation the live source now refuses (``REFUSED_CODES``) is inaccessible
    too, by its own name, although a copy of it is held: the copy is only stale.
    How a source was served (stale, inaccessible) comes before what is unknown."""
    gaps = []
    for item in freshness:
        if item.get("stale"):
            gaps.append(_gap("stale", "stale_source", item["source"],
                             item.get("label") or
                             "the source's last capture is older than its freshness bound"))
        elif item.get("completeness") == "unknown":
            gaps.append(_gap("unknown", "freshness_unknown", item["source"],
                             "the projection does not state this source's capture state"))
        if item.get("completeness") == "partial" and item["source"] in (elsewhere or {}):
            # read only in part, but only in relations holding none of the answer's
            # records (``OntologyTools._partly_read_elsewhere``): no record of it is missing
            unread = ", ".join(elsewhere[item["source"]][:8])
            gaps.append(_gap("unknown", "partly_read_elsewhere", item["source"],
                             "this source's last capture read some relations only in "
                             f"part ({unread}); none of them holds records of this answer, "
                             "whose relations were read whole"[:600]))
        elif item.get("completeness") == "partial":
            # a source read only in part (a relation not traversed whole) may hold records
            # no answer reached: never an age-only gap, so a count over it stays a lower
            # bound (claim_checks.AGE_ONLY_GAPS)
            gaps.append(_gap("unknown", "source_partly_read", item["source"],
                             "this source's last capture read it only in part; records "
                             "it holds may be missing, so a count over it is a lower "
                             "bound"))
        for unit in item.get("relations", ()):
            if unit.get("served_from") in COPIED and unit.get("error_code") in REFUSED_CODES:
                gaps.append(_gap("inaccessible", unit["error_code"], unit["relation"],
                                 f"the live source refuses this relation now; its records "
                                 f"are a copy as of {unit.get('captured_at') or 'unknown'}"))
        missing = [u for u in item.get("relations", ()) if u.get("served_from") == "none"]
        if missing and all(u.get("error_code") == "not_captured" for u in missing):
            # ontology.resolve.NOT_CAPTURED: a host with no live client whose own capture
            # holds none of them; the live source was not asked.
            gaps.append(_gap("inaccessible", "source_unavailable", item["source"],
                             f"this host's capture holds none of {len(missing)} "
                             "relation(s) and this host reads no live source: "
                             + ", ".join(u["relation"] for u in missing)))
        elif missing:
            failures = sorted({f"{u.get('http_status') or 'no_http_status'}, "
                               f"{u.get('error_code') or 'unknown'}" for u in missing})
            gaps.append(_gap("inaccessible", "source_unavailable", item["source"],
                             f"live source unavailable ({'; '.join(failures)}) and no "
                             f"persisted capture holds {len(missing)} relation(s): "
                             + ", ".join(u["relation"] for u in missing)))
    return sorted(gaps, key=lambda gap: gap["state"] == "unknown")


def _freshness_first(fresh: Sequence[Mapping[str, Any]],
                     rest: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Freshness gaps first and never cut; the others fill what is left of MAX_GAPS."""
    return [dict(g) for g in fresh] + [dict(g) for g in rest][:max(0, MAX_GAPS - len(fresh))]


def _column_path(field_path: str) -> str:
    """The source path of a projected field path (``row.<path>`` / ``rows.<n>.<path>``)."""
    head, _, rest = field_path.partition(".")
    if head == "rows":
        rest = rest.partition(".")[2]
    return rest


def _cited(row: Mapping[str, Any], value: Any,
           spec: Mapping[str, Any] | None = None) -> Any:
    """``ontology.resolve.CitedValue`` of one store row, or None when it cannot be cited.
    ``spec``: the declared property of a money row (its tax basis, ``value_basis``)."""
    from ontology.resolve import CitedValue
    from ontology.units import is_unit_price, value_basis
    citation = _citation(row)
    if citation is None or value is None:
        return None
    keys = row.keys()
    binding = row["binding_id"] if "binding_id" in keys else None
    basis = None
    if spec is not None and spec.get("value_type") == "money" and binding is not None:
        basis = value_basis(spec, row)
    return CitedValue(str(value), citation["l1_handle"], citation["field_path"],
                      citation["digest"], citation["lineage"],
                      row["currency"] if "currency" in keys else None,
                      row["unit"] if "unit" in keys else None, basis, binding,
                      is_unit_price(spec))


def _cited_values(rows: Iterable[Mapping[str, Any]], prop: str, value_type: str,
                  spec: Mapping[str, Any] | None = None) -> list[Any]:
    out = []
    for row in rows:
        if (row["property"] == prop and row["value_type"] == value_type
                and row["value"] is not None):
            value = _cited(row, row["value"], spec)
            if value is not None:
                out.append(value)
    return out


def _corroboration_features(verdict: str, checks: Sequence[Any], counterparts: int
                            ) -> list[dict[str, Any]]:
    """The identity verdict feature, then one ``corroboration_<property>`` feature per
    identifier comparison (conflicts first), then the shared values apart:
    ``value_differs`` / ``value_equal`` / ``value_not_comparable`` (agrees false / true
    / null), which never change the verdict. Each cites the compared values of both
    sides (counterpart first)."""
    def cites(check: Any) -> list[dict[str, Any]]:
        return [{"l1_handle": v.l1_handle, "field_path": v.field_path, "digest": v.digest,
                 "lineage": v.lineage} for v in (*check.counterpart, *check.candidate)][:4]

    keys = [c for c in checks if c.basis == "linked_identifier"]
    values = [c for c in checks if c.basis != "linked_identifier"]
    deciding = [c for c in keys if c.verdict == verdict]
    head: dict[str, Any] = {"feature": CORROBORATION_FEATURE,
                            "agrees": _VERDICT_AGREES[verdict]}
    if verdict != "unverifiable" and deciding:
        head["citations"] = [c for check in deciding for c in cites(check)][:4]
    out = [head]
    if not counterparts:
        out.append({"feature": "no_visible_counterpart", "agrees": None})
    order = {"conflict": 0, "differs": 0, "agree": 1, "unverifiable": 2}
    named = {"differs": ("value_differs", False), "agree": ("value_equal", True),
             "unverifiable": ("value_not_comparable", None)}
    for check in sorted(keys, key=lambda c: (order[c.verdict], c.feature)):
        feature: dict[str, Any] = {"feature": f"corroboration_{check.feature}"[:64],
                                   "agrees": _VERDICT_AGREES[check.verdict]}
        if cites(check):
            feature["citations"] = cites(check)
        out.append(feature)
    for check in sorted(values, key=lambda c: (order[c.verdict], c.feature)):
        name, agrees = named[check.verdict]
        feature = {"feature": name, "agrees": agrees}
        if cites(check):
            feature["citations"] = cites(check)
        out.append(feature)
    return out[:15]


def corroboration_verdict(candidate: Mapping[str, Any]) -> str:
    """``agree`` / ``conflict`` / ``unverifiable`` of one plx.find candidate (its
    ``corroboration`` feature; a candidate without one is ``unverifiable``)."""
    for feature in candidate.get("why_not", ()) or ():
        if feature.get("feature") == CORROBORATION_FEATURE:
            agrees = feature.get("agrees")
            return "agree" if agrees is True else "conflict" if agrees is False \
                else "unverifiable"
    return "unverifiable"


def _ceiling(link: Mapping[str, Any]) -> str | None:
    ceilings = [b.get("tier_ceiling") for b in link.get("bindings", ()) if b.get("tier_ceiling")]
    return min(ceilings, key=lambda t: _TIER_RANK[t]) if ceilings else None


def _completeness(returned: int, total: int | None, truncated: bool, reason: str | None,
                  next_cursor: str | None) -> dict[str, Any]:
    out: dict[str, Any] = {"complete": not truncated, "truncated": truncated,
                           "returned": returned, "total": total, "next_cursor": next_cursor}
    if truncated:
        out["reason"] = reason or "truncated"
    return out


def _fingerprint(arguments: Mapping[str, Any], actor: Actor) -> str:
    # A mention window (plx.documents ``mention_offset``) pages the same listing.
    body = {k: v for k, v in arguments.items()
            if k not in ("cursor", "limit", "mention_offset")}
    body["actor"] = [actor.tenant, sorted(actor.principals)]
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]


def _write_cursor(offset: int, at: dt.datetime, fingerprint: str,
                  mode: str | None = None) -> str:
    """``mode``: the ranking the page belongs to (``operating/search/plug.Ranked.mode``),
    kept so a later page is listed from that same ranking or refused."""
    body: dict[str, Any] = {"o": offset, "t": _format_time(at), "f": fingerprint}
    if mode is not None and mode != "index":
        body["m"] = mode
    raw = json.dumps(body, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _generation(store_meta: Mapping[str, str]) -> str:
    """The projection generation a scan cursor is bound to: the store's content digest
    (``StoreBuilder.commit``), else its build time; empty when the store states neither."""
    return str(store_meta.get("content_digest") or store_meta.get("built_at") or "")[:80]


def _scan_check(body: Mapping[str, Any]) -> str:
    """An integrity check over every field of a scan cursor (not a secret: a forged
    position only moves where the scan of readable objects resumes)."""
    return hashlib.sha256(json.dumps({k: body[k] for k in sorted(body) if k != "h"},
                                     sort_keys=True).encode()).hexdigest()[:16]


def _read_scan_cursor(cursor: str, fingerprint: str, generation: str
                      ) -> tuple[int, dt.datetime, str, int, frozenset[str], tuple[str, ...]]:
    """(offset, pinned time, scan position, prior matches, undecided properties, last
    listed ids) of a filter listing's cursor; ``invalid_cursor`` for one of another
    query, reader or projection generation, or with any field edited."""
    try:
        if not cursor.startswith(SCAN_CURSOR_PREFIX):
            raise ValueError
        payload = cursor[len(SCAN_CURSOR_PREFIX):]
        padded = payload + "=" * (-len(payload) % 4)
        inflater = zlib.decompressobj()
        raw = inflater.decompress(base64.urlsafe_b64decode(padded.encode()), 4096)
        if inflater.unconsumed_tail or not inflater.eof:
            raise ValueError
        body = json.loads(raw.decode())
        offset, at, after, prior = (body["o"], _parse_time(body["t"]), body["a"],
                                    body["n"])
        undecided, last = body["u"], body["k"]
        if (body["f"] != fingerprint or body["g"] != _generation_tag(generation)
                or body["h"] != _scan_check(body) or type(offset) is not int or offset < 0
                or at is None or not isinstance(after, str) or type(prior) is not int
                or prior < 0 or not isinstance(undecided, list) or not isinstance(last, list)
                or (not generation and (after or prior))
                or len(last) > 2 or len(last) > prior
                or not all(isinstance(u, str) for u in undecided + last)):
            raise ValueError
    except (ValueError, KeyError, TypeError, AttributeError, zlib.error):
        raise ToolError("invalid_cursor", "the cursor does not belong to this query, "
                        "reader or projection generation",
                        "repeat the query without cursor") from None
    return offset, at, after, prior, frozenset(undecided), tuple(last)


def _generation_tag(generation: str) -> str:
    return hashlib.sha256(generation.encode()).hexdigest()[:16]


def _cursor_mode(cursor: str) -> str | None:
    """The ranking mode a (validated) cursor was written for; None: the word index."""
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        mode = json.loads(base64.urlsafe_b64decode(padded.encode()).decode()).get("m")
    except (ValueError, AttributeError):
        return None
    return mode if isinstance(mode, str) else None


def _read_cursor(cursor: str, fingerprint: str) -> tuple[int, dt.datetime]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        body = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
        offset, at = int(body["o"]), _parse_time(body["t"])
        if body["f"] != fingerprint or offset < 0 or at is None:
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise ToolError("invalid_cursor", "the cursor does not belong to this query",
                        "repeat the query without cursor") from None
    return offset, at


def _not_found_subject(query: str | None, object_type: str | None, filters: list[dict]) -> str:
    if query is not None:
        typed = f"{object_type} " if object_type else ""
        return f"{typed}query:{' '.join(query.split())}"[:700]
    return f"{object_type} where " + ", ".join(f"{f['property']} {f['op']}" for f in filters)


def _withheld_filter_gap(object_type: str | None, properties: Iterable[str]) -> dict[str, Any]:
    """The gap of a filter that some readable record could not be decided on."""
    subject = ", ".join(f"{object_type}.{p}" if object_type else p for p in sorted(properties))
    return _gap("unknown", "marking_withheld", subject[:700],
                "some records this reader can read hold a value of a filtered property it "
                "may not read; they are neither matched nor excluded on it (a withheld "
                "value is not a missing one)")


UNDATED_MARK = "date_not_on_record:"


def _parse_day(text: str) -> dt.date | None:
    try:
        return dt.date.fromisoformat(text[:10])
    except ValueError:
        return None


def undated_marker(prop: str) -> str:
    """The undecided-set entry of a filtered date property some record holds only as a
    bulk default (``OntologyTools._undated_defaults``): never a property name."""
    return UNDATED_MARK + prop


def _target_marks(hidden: Iterable[str]) -> set[str]:
    """The link-target provenance marks among ``_linked_ids``' undecided entries."""
    return {h for h in hidden
            if h.startswith((LINK_TARGETS_PARTIAL, LINK_TARGETS_LEAD, LINK_BELOW_FLOOR))}


def _filter_gaps(object_type: str | None, undecided: Iterable[str]) -> list[dict[str, Any]]:
    """The gaps of a filter undecided on some readable records: a withheld value
    (``_withheld_filter_gap``), or a date that is only its source's bulk default with no
    origin stating another (``date_not_on_record``). Either way the records are neither
    matched nor excluded and the answer is partial."""
    undecided = set(undecided)
    undated = sorted(p[len(UNDATED_MARK):] for p in undecided if p.startswith(UNDATED_MARK))
    partial = sorted(p[len(LINK_TARGETS_PARTIAL):] for p in undecided
                     if p.startswith(LINK_TARGETS_PARTIAL))
    lead = sorted(p[len(LINK_TARGETS_LEAD):] for p in undecided
                  if p.startswith(LINK_TARGETS_LEAD))
    below = sorted(p[len(LINK_BELOW_FLOOR):] for p in undecided
                   if p.startswith(LINK_BELOW_FLOOR))
    withheld = sorted(p for p in undecided if not p.startswith(
        (UNDATED_MARK, LINK_TARGETS_PARTIAL, LINK_TARGETS_LEAD, LINK_BELOW_FLOOR)))
    out = [_withheld_filter_gap(object_type, withheld)] if withheld else []

    def named(props: Sequence[str]) -> str:
        return ", ".join(f"{object_type}.{p}" if object_type else p for p in props)[:700]
    if partial:
        out.append(_gap("unknown", "set_incomplete", named(partial),
                        "a link filter names a set that does not hold every match (or that "
                        "this reader's grants trimmed): the records it keeps are at least "
                        "these, never all there are (QUERY-005)"))
    if lead:
        out.append(_gap("unknown", "lead_basis", named(lead),
                        "a link filter names a set reached only through lead-tier links: "
                        "the records it keeps are likely matches, never confirmed ones; say "
                        "them as a lead"))
    if below:
        out.append(_gap("unknown", "below_tier_floor", named(below),
                        "some records are joined to the named ones only by lead-tier links "
                        "(a text join, a staging record): they are neither counted nor "
                        "excluded; plx.around with tier_floor lead lists them (QUERY-007)"))
    if undated:
        subject = ", ".join(f"{object_type}.{p}" if object_type else p for p in undated)
        out.append(_gap("unknown", "date_not_on_record", subject[:700],
                        "some records hold only a day a bulk share of their source's "
                        "records hold that their origin record contradicts or that is the "
                        "record's own creation or migration stamp (an import or migration "
                        "day), and no origin states another: their date is not on record, "
                        "so they are neither matched nor excluded by the date filter"))
    return out


_NUMERIC_TYPES = "('decimal', 'money', 'quantity', 'integer')"
# outside ASCII (a byte past one per character): read alike by SQLite and Postgres, whose
# GLOB translation reads a negated class as a plain one
_NOT_ASCII = "octet_length(x.value) > length(x.value)"
_ISO_DAY = "x.value GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'"


def _business_day(text: str) -> str:
    """An ISO time as its business day (``YYYY-MM-DD`` in ``time_window.BUSINESS_TZ``):
    a time with a zone or offset converted, one without read as written; text that is no
    time stays as it is."""
    if _DATE.match(text):
        return text
    try:
        moment = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if moment.tzinfo is not None:
        from zoneinfo import ZoneInfo
        from operating.time_window import BUSINESS_TZ
        moment = moment.astimezone(ZoneInfo(BUSINESS_TZ))
    return moment.date().isoformat()


def _event_bound(f: Mapping[str, Any]) -> str | None:
    """The ISO day an event filter's ``gte``/``lt`` bound is widened to (two days, as a
    value bound: ``_value_narrowing``); None for any other filter or an unreadable
    bound (the event's presence alone narrows)."""
    wanted = f.get("value")
    wanted = wanted[0] if isinstance(wanted, list) and wanted else wanted
    moment = _parse_time(wanted) if isinstance(wanted, str) else None
    if f.get("op") not in ("gte", "gt", "lt", "lte") or moment is None:
        return None
    return (moment.date() + dt.timedelta(days=-2 if f["op"] in ("gte", "gt") else 2)
            ).isoformat()


def _filters_verdict(values: Mapping[str, list[tuple[str, Any]]], withheld: Iterable[str],
                     filters: Sequence[Mapping[str, Any]]) -> tuple[bool | None, set[str]]:
    """Every filter on one object: (True, False or None when no filter fails but one
    is undecided, the properties of the undecided filters)."""
    withheld = set(withheld)
    # QUERY-008: bounds on one property (gte 50 and lte 70; an event after Feb 1 and
    # before Mar 1) are one interval: one value, or one occurrence, must lie in all of
    # them, never a low one meeting the lower bound and a high one the upper
    bounded: dict[str, list[Mapping[str, Any]]] = {}
    for f in filters:
        if f.get("op") in _RANGE_OPS:
            bounded.setdefault(f["property"], []).append(f)
    grouped = {prop for prop, group in bounded.items() if len(group) > 1}
    verdicts = [(_filter_holds(values.get(f["property"], []), f, f["property"] in withheld),
                 f["property"]) for f in filters if f["property"] not in grouped]
    for prop in sorted(grouped):
        verdicts.append((_interval_holds(values.get(prop, []), bounded[prop],
                                         prop in withheld), prop))
    if any(v is False for v, _ in verdicts):
        return False, set()
    unsure = {p for v, p in verdicts if v is None}
    return (None if unsure else True), unsure


_RANGE_OPS = frozenset({"gt", "gte", "lt", "lte"})


def _interval_holds(values: list[tuple[str, Any]], bounds: Sequence[Mapping[str, Any]],
                    withheld: bool = False) -> bool | None:
    """Whether one readable value of a property meets every range filter ``bounds``
    on it (``_filter_holds`` per value): True when one does; None when none does but
    one could (a bound undecidable on it, or a value the reader may not read); else
    False."""
    undecided = withheld
    for value in values:
        if value[1] is None:
            continue
        held = [_filter_holds([value], bound) for bound in bounds]
        if all(h is True for h in held):
            return True
        if not any(h is False for h in held):
            undecided = True
    return None if undecided else False


def _filter_holds(values: list[tuple[str, Any]], spec: Mapping[str, Any],
                  withheld: bool = False) -> bool | None:
    """Whether one filter holds on the values of a property this reader can read.
    ``withheld``: the object also holds a value of the property the reader may not
    read; a verdict that value could change is then None (undecided), never a match
    or a miss: a withheld value is never read as missing (``absent``, ``ne``) nor
    matched on."""
    op, target = spec["op"], spec.get("value")
    present = [(t, v) for t, v in values if v is not None]
    unsure = None if withheld else False
    if op == "present":
        return True if present else unsure
    if op == "absent":
        return False if present else (None if withheld else True)
    targets = target if isinstance(target, list) else [target]

    def equal(value_type: str, value: Any, wanted: Any) -> bool:
        if isinstance(wanted, bool):
            return value == ("true" if wanted else "false")
        return _compare_key(value_type, value) == _compare_key(value_type, wanted)

    def order(value_type: str, value: Any, wanted: Any, day_end: bool = False) -> int | None:
        if value_type in ("decimal", "money", "quantity", "integer"):
            try:
                a, b = Decimal(str(value)), Decimal(str(wanted))
            except InvalidOperation:
                return None
        elif value_type in ("date", "timestamp"):
            # a bare day bounds the whole day: "on or before 2026-11-02" (``lte``) holds a
            # time later that day, "after" it (``gt``) none of them (dev 11-10: 412 of 500
            # approvals requested on or before a day, the day's own left out)
            a, b = _parse_time(str(value)), _parse_time(str(wanted), end_of_day=day_end)
            if a is None or b is None:
                return None
        else:
            a, b = str(value), str(wanted)
        return (a > b) - (a < b)

    if op in ("eq", "in"):
        return any(equal(t, v, w) for t, v in present for w in targets) or unsure
    if op == "ne":
        if any(equal(t, v, w) for t, v in present for w in targets):
            return False
        return None if withheld else bool(present)
    if op == "prefix":
        return any(str(v).casefold().startswith(str(w).casefold()) for _, v in present
                   for w in targets) or unsure
    if op == "contains":
        # the text holds the wanted words anywhere, any case ("whose name mentions")
        def spaced(text: Any) -> str:
            return " ".join(str(text).casefold().split())
        return any(spaced(w) in spaced(v) for _, v in present
                   for w in targets if spaced(w)) or unsure
    if op in ("gte", "gt", "lt", "lte"):
        holds = {"gte": lambda sign: sign >= 0, "gt": lambda sign: sign > 0,
                 "lt": lambda sign: sign < 0, "lte": lambda sign: sign <= 0}[op]
        for value_type, value in present:
            sign = order(value_type, value, targets[0], day_end=op in ("lte", "gt"))
            if sign is not None and holds(sign):
                return True
        return unsure
    return False


def _index_row_ok(row: Mapping[str, Any], *, binding_key: str = "binding_id",
                  kind_key: str | None = "identifier_kind") -> bool:
    """An index row the contract can show (a malformed projector row is skipped,
    never echoed in a shape that breaks the answer)."""
    return (bool(_NAME.match(row["property"] or ""))
            and bool(re.match(r"^[a-z][a-z0-9_.-]{1,95}$", row[binding_key] or ""))
            and (kind_key is None or bool(_NAME.match(row[kind_key] or "")))
            and bool(_OBJECT_ID.match(row["object_id"] or ""))
            and bool(_HANDLE.match(row["l1_handle"] or "")))


def _name_words(text: Any) -> list[str]:
    """The words of ``text``, case folded, punctuation and spacing apart."""
    return re.findall(r"[^\W_]+", text.casefold()) if isinstance(text, str) else []


def _names_query(value: Any, asked: Sequence[str]) -> bool:
    """Whether a display ``value`` names the query (its words ``asked``): the value's words
    are the query's, or begin with them ("Pet Alliance" names "PET ALLIANCE INC.")."""
    held = _name_words(value)
    return bool(asked) and held[:len(asked)] == list(asked)


def _value_text_match(row: Mapping[str, Any], reason: str, needle: str) -> _Match | None:
    """A value the word index matched, as a lead (None: a row the contract cannot show)."""
    if not _index_row_ok(row, binding_key="object_binding", kind_key=None):
        return None
    return _Match(row["object_id"], "lead", reason, "primary", "display_text", needle[:200],
                  row["value"], row["property"], row["object_binding"], row["l1_handle"],
                  row["field_path"])


# Characters of a captured document's text quoted around the first matched word.
TEXT_SNIPPET_CHARS = 300


def _chunk_match(row: Mapping[str, Any], version: Mapping[str, Any], needle: str,
                 tokens: Sequence[str]) -> _Match | None:
    """A chunk of captured document text the word index matched (``chunk_matches``), as a
    lead cited to the field's own page and path and quoting the text around its first
    matched word; None when the object's readable version is not the one the field reads
    or the row cannot be shown. ``property`` names the field (the last part of its path)."""
    name = row["field_path"].rsplit(".", 1)[-1].lower()
    if version["l1_handle"] != row["version_handle"] or not _FIELD_PATH.match(
            row["field_path"]) or not _HANDLE.match(row["citation"] or ""):
        return None
    shown = {"object_id": row["object_id"], "property": name if _NAME.match(name) else "text",
             "object_binding": row["object_binding"], "l1_handle": row["citation"]}
    if not _index_row_ok(shown, binding_key="object_binding", kind_key=None):
        return None
    text = chunk_text(row["body"])
    found = re.search(r"(?<![^\W_])" + re.escape(tokens[0]), text, re.IGNORECASE)
    start = max(0, (found.start() if found else 0) - TEXT_SNIPPET_CHARS // 3)
    return _Match(row["object_id"], "lead", "document_text", "primary", "display_text",
                  needle[:200], text[start:start + TEXT_SNIPPET_CHARS], shown["property"],
                  row["object_binding"], row["citation"], row["field_path"],
                  sender_class=row["sender_class"])


def unit_out(raw: Any) -> str | None:
    """A projected unit as a tool emits it: as the source spells it when that is a
    ``$defs/unit`` token, else its Portal fold (``ontology.units.unit_token``) when that
    is; None when neither is (the caller states a ``unit_invalid`` gap quoting the cell)."""
    from ontology.units import unit_token
    if raw is None or is_unit(raw):
        return raw
    folded = unit_token(raw)
    return folded if is_unit(folded) else None


def unit_text(raw: Any) -> str:
    """A unit as a Workbench sentence (note, detail, message) names it: its ``unit_out``
    form, or a placeholder for a cell that is no unit. A sentence never quotes a source
    cell; the cell is source text only inside the cited value's ``unit_gap`` envelope."""
    if raw is None or raw == "":
        return "no unit"
    shown = unit_out(raw)
    return shown if shown is not None else "an invalid unit"


def units_text(units: Iterable[Any]) -> str:
    """Several units as one sentence names them (``unit_text`` each, sorted, once)."""
    return ", ".join(sorted({unit_text(u) for u in units}))


def _citation_text(text: str) -> dict[str, Any] | None:
    """The ``$defs/value_citation`` a stored citation (canonical JSON) names, or None."""
    try:
        cite = json.loads(text)
    except ValueError:
        return None
    keys = ("l1_handle", "field_path", "digest", "lineage")
    if not isinstance(cite, dict) or not all(isinstance(cite.get(k), str) for k in keys):
        return None
    return _citation({k: cite[k] for k in keys})


_BAD = object()


def _edge(row: Mapping[str, Any], detailed: bool) -> dict[str, Any] | None:
    def features(text: str) -> list[dict] | None:
        try:
            items = json.loads(text)
        except ValueError:
            return None
        out = []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict) or not _NAME.match(str(item.get("feature", ""))):
                return None
            feature: dict[str, Any] = {"feature": item["feature"],
                                       "agrees": item.get("agrees")
                                       if isinstance(item.get("agrees"), bool) else None}
            if isinstance(item.get("weight"), (int, float)) and not isinstance(
                    item.get("weight"), bool):
                feature["weight"] = item["weight"]
            if detailed:
                cites = [c for c in item.get("citations", ()) if isinstance(c, dict)
                         and _citation_dict_ok(c)][:4]
                if cites:
                    feature["citations"] = cites
            out.append(feature)
        return out[:16]

    reason, why_not = features(row["reason"]), features(row["why_not"])
    if not reason or why_not is None or not _LINK_TYPE.match(row["link_type"]) \
            or not _OBJECT_ID.match(row["from_object"]) or not _OBJECT_ID.match(row["to_object"]):
        return None
    edge = {"link_id": row["link_id"][:128], "link_type": row["link_type"],
            "from": row["from_object"], "to": row["to_object"], "tier": row["tier"],
            "reason": reason, "why_not": why_not}
    markings = list(_markings(row["markings"]))
    if markings:
        edge["markings"] = markings
    return edge


def _citation_dict_ok(item: Mapping[str, Any]) -> bool:
    return (set(item) == {"l1_handle", "field_path", "digest", "lineage"}
            and bool(_HANDLE.match(str(item["l1_handle"])))
            and bool(_FIELD_PATH.match(str(item["field_path"])))
            and bool(_DIGEST.match(str(item["digest"]))))


def _resolve_conflicts(conflicts: list[dict], first_seen: dict[tuple[str, str], Any],
                       peer_values: Sequence[tuple[str, Mapping]]) -> None:
    """Each value disagreement's ``resolution`` (``operating.answer_conflicts``): the
    values the system of record, then the newest reading, then the most specific
    document trusts, the rule and why; none trusted when no rule decides. Over the
    values this reader already reads (the conflict holds no other)."""
    from operating.answer_conflicts import resolution
    dated = dict(first_seen)
    for _, row in peer_values:
        dated.setdefault((row["l1_handle"], row["field_path"]), row["recorded_at"])
    for conflict in conflicts:
        if conflict.get("kind") == "value_disagreement" and conflict.get("values"):
            found = resolution(conflict, dated=dated)
            if found is not None:
                conflict["resolution"] = found


# --------------------------------------------------------------------------- host wiring

# Retirement plan for the case host's tool budget (contract limits.max_case_host_tools).
# Each retired tool names a successor that exists now (one of these tools or a
# kept host tool), never a contract name still unbuilt. The lead's decision in review l8-1. The plan leaves
# the host above the budget.
HOST_RETIREMENTS: dict[str, dict[str, str]] = {
    # Source reads and kind tools retire into the
    # generic tools; their successors are on the case host now.
    "plx.source_resolve": {"successor": "plx.find", "basis": "directive"},
    "plx.source_catalog": {"successor": "plx.schema", "basis": "lead"},
    "plx.source_rows": {"successor": "plx.object", "basis": "lead"},
    "plx.live_source_list": {"successor": "plx.find", "basis": "lead"},
    "plx.case_customer_po_coverage": {"successor": "plx.find", "basis": "lead"},
    "plx.bc_purchase_case": {"successor": "plx.find", "basis": "lead"},
    "plx.case_propose_customer_po": {"successor": "plx.case_propose", "basis": "lead"},
    "plx.case_record_customer_po_gaps": {"successor": "plx.case_record_gap", "basis": "lead"},
    # Freshness rides on every answer instead (``_meta`` of each tools/call result).
    "plx.ingest_status": {"successor": "_meta", "basis": "lead"},
    # Case-host-budget (b): one tool, two modes.
    "plx.case_list": {"successor": "plx.case_get", "basis": "merge"},
    "plx.case_check_claims": {"successor": "plx.case_get", "basis": "merge"},
    "plx.case_reextract_original": {"successor": "plx.case_attach_original", "basis": "merge"},
    "plx.action_list": {"successor": "plx.act", "basis": "merge"},
}
# All but plx.source_resolve left with the
# platform host (deletion-6) and must never return to the case host.
DIRECTIVE_RETIRED = frozenset({
    "plx.search", "plx.transaction_search", "plx.ask_transaction", "plx.transaction_inquiry",
    "plx.source_resolve", "plx.lookup_replica_named", "plx.discover_fm_procurement"})


def item_gate_from_case_runtime(runtime: Any) -> Callable[[Actor, str], bool]:
    """The case host's own per-reader citation check as an item gate: a record is
    readable when ``CaseRuntime.resolve_citation(handle, actor)`` resolves it
    (mailbox scope and owner ACL, SharePoint item permissions, original audience;
    operating/case_runtime.py). Any refusal or failure denies."""
    def gate(actor: Actor, handle: str) -> bool:
        try:
            runtime.resolve_citation(handle, actor)
        except Exception:  # noqa: BLE001 - unresolvable for this reader: deny
            return False
        return True
    gate.key_material = lambda actor: runtime_key_material(runtime, actor)
    # the runtime whose functions decide, so a bulk reader (``plx.table``'s mail and Teams
    # rules, ``ontology_tools_table_items``) computes each reader's parts with them
    gate.runtime = runtime
    return gate


def runtime_key_material(runtime: Any, actor: Any) -> dict[str, Any] | None:
    """What decides ``item_gate_from_case_runtime``'s gate for ``actor`` beyond the host's
    stores (``OntologyTools.item_gate_scope``): the actor, the employee entitlement the
    runtime resolves for it (its fingerprint changes with any change in access) and the
    people exclusion's list. The item audience keys (``operating.pg.item_audiences``) are
    digests of it, so a reader whose access or the exclusion changed holds a new key no
    recorded decision covers. None, never a key, when it cannot be told: the exclusion is
    not usable, the entitlement does not resolve, or the actor names a person the
    exclusion names (or one it cannot decide)."""
    from integrations.people_exclusion import PeopleExclusion
    if not isinstance(actor, Actor):
        return None
    guard = getattr(runtime, "people_exclusion", None)
    if not isinstance(guard, PeopleExclusion) or guard.unavailable:
        return None
    try:
        for principal in actor.principals:
            tail = principal.rpartition(":")[2].strip()
            if "@" in tail or _is_object_id(tail):
                if not guard.decides(tail) or guard.is_excluded(tail):
                    return None
        gate = getattr(runtime, "employee_gate", None)
        entitlement = None if gate is None else gate.entitlement(actor)
    except Exception:  # noqa: BLE001 - an undecidable reader holds no key
        return None
    fingerprint = None if entitlement is None else getattr(entitlement, "fingerprint", None)
    if entitlement is not None and not fingerprint:
        return None
    material = {"actor": [actor.tenant, sorted(actor.principals), actor.clearance],
                "entitlement": fingerprint, "exclusion": guard.fingerprint()}
    if entitlement is not None:
        # The staff item rule's own inputs (restricted sites, shared mailboxes, the switch):
        # a change gives every staff reader a new key, so no recorded decision outlives it.
        try:
            held = getattr(runtime, "staff_key_material", None)
            staff = None if held is None or entitlement.unrestricted() else held()
        except Exception:  # noqa: BLE001 - an undecidable staff scope holds no key
            return None
        if staff is not None:
            material["staff"] = staff
    return material


def _is_object_id(value: str) -> bool:
    from integrations.people_exclusion import is_object_id
    return is_object_id(value.lower())


def _inline_defs(schema: Any, contracts: Mapping[str, Any]) -> dict[str, Any]:
    """The tool's input schema with every ``$defs`` entry it references, so an MCP
    client can resolve its ``#/$defs/...`` refs without the contract file."""
    needed: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                name = ref.split("/")[2]
                if name not in needed:
                    needed.add(name)
                    walk(contracts["$defs"][name])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(schema)
    out = dict(schema)
    if needed:
        out["$defs"] = {name: contracts["$defs"][name] for name in sorted(needed)}
    return out


def tool_specs(contracts: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """MCP tool listings (name, description, inputSchema) for these tools."""
    contracts = contracts or load_contracts()
    return [{"name": name, "description": contracts["tools"][name]["description"],
             "inputSchema": _inline_defs(contracts["tools"][name]["input"], contracts),
             "annotations": {"readOnlyHint": True}}
            for name in TOOL_NAMES]


def _fit_entities(result: dict[str, Any], limit: int) -> None:
    """Keep a result's ``entities`` within the response bound: lead entities go first
    (from the last), then each entity keeps fewer records (``more_records`` counts the
    rest); the matched things stay named."""
    entities = result.get("entities")
    if not entities:
        return
    bound = int(limit * 0.9)
    while _envelope_bytes(result) > bound and len(entities) > 1 \
            and entities[-1].get("status") == "lead":
        entities.pop()
    keep = 8
    while _envelope_bytes(result) > bound and keep >= 1:
        for entity in entities:
            records = entity["records"]
            if len(records) > keep:
                entity["more_records"] = entity.get("more_records", 0) + len(records) - keep
                entity["records"] = records[:keep]
        keep = keep // 2 if keep > 1 else 0
    while _envelope_bytes(result) > bound and len(entities) > 1:
        entities.pop()


def register(tool_table: MutableMapping[str, dict[str, Any]], tools: OntologyTools) -> None:
    """Add ``plx.schema``/``plx.find``/``plx.object`` to a case host's tool table:
    name -> {name, description, inputSchema, annotations, handler}. ``handler``
    is ``handler(arguments, actor) -> result``; the host passes its session actor.
    A name already present is refused (no silent shadowing)."""
    for spec in tool_specs(tools.contracts):
        name = spec["name"]
        if name in tool_table:
            raise ValueError(f"tool already registered: {name}")
        tool_table[name] = dict(spec, handler=(lambda arguments, actor, _n=name:
                                               tools.call(_n, arguments, actor)))
