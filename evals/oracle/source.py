"""Raw source reads for the oracle: injected ports, cached reads, indexes, generations.

The oracle never opens a connection itself. The caller injects:

* a Portal port, ``read_relation(name, columns) -> page-read result`` with the shape of
  ``integrations.workbench_read_api.WorkbenchReadApiClient.read_relation`` (``rows``,
  ``key``, ``catalog_revision``, ``health_before.generated_at``); live, that is the
  GET-only reporting API client, wired by ``scripts/eval_oracle_dev.py``;
* optionally a BC port (``BcPort`` below), live the GET-only BC reader;
* optionally a document port for the mail/SharePoint strata of the dev-set generator.

A read that fails is recorded (error class only) and every answer that needed it reports
an ``inaccessible`` gap; it is never read as absence. Values are compared as exact text
(no trimming, no case folding): a person's identifier either equals the stored key or it
does not.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import re
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

from .keys import RELATIONS

_INTEGRAL = re.compile(r"^-?[0-9]+$")
_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,9}))?)?"
                  r"(Z|[+-]\d{2}:?\d{2})?)?$")
_YYYYMMDD = re.compile(r"^(\d{4})(\d{2})(\d{2})$")


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      default=str)


def digest(value: Any) -> str:
    return sha256(canonical(value).encode("utf-8")).hexdigest()


def distinct(rows: Iterable[Any], keyed: Callable[[Any], bool]) -> list[Any]:
    """Rows in order without exact repeats of a keyed row: a record a paged or replayed
    read delivered twice carries its source key twice, so it is one record and a repeated
    event is never counted twice. A row without its source key (``keyed`` false: the
    relation declares none, or the row lacks a key value) always stays, since two equal
    keyless rows may be two real events; rows that differ in any value all stay."""
    seen: set[str] = set()
    out = []
    for row in rows:
        if keyed(row):
            text = canonical(row)
            if text in seen:
                continue
            seen.add(text)
        out.append(row)
    return out


def portal_keyed(key: Any) -> Callable[[Mapping[str, Any]], bool]:
    """Whether a Portal row carries its read's key (``key``: the key columns the read
    declares; none declared, nothing is keyed)."""
    columns = [c for c in key if isinstance(c, str)] if isinstance(key, list) else []
    return lambda row: bool(columns) and all(key_text(row.get(c)) is not None
                                             for c in columns)


def bc_keyed(record: Mapping[str, Any]) -> bool:
    """Whether a BC record carries its key (company and system id)."""
    return key_text(record.get("company_id")) is not None and \
        key_text(record.get("id")) is not None


def key_text(value: Any) -> str | None:
    """Join-key text: integers and integral floats as digits, strings exactly as stored."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    if isinstance(value, str):
        return value if value != "" else None
    return None


def sql_trim(text: str) -> str:
    """SQL ``TRIM(x)`` as Portal's queries use it: leading and trailing spaces only (a tab or
    newline stays, so such a number does not match in Portal and must not here)."""
    return text.strip(" ")


def decimal_of(value: Any) -> Decimal | None:
    """Quantities arrive as float-formatted strings or JSON numbers (03 §6 trap 7)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def decimal_text(value: Decimal) -> str:
    """Canonical decimal text: no exponent, no trailing zeros ("1920", "12.5")."""
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def instant(value: Any) -> datetime | None:
    """A UTC instant from ISO text (naive = UTC, 03 §6 timestamp-utc) or Sage yyyymmdd.
    Year 1 is no date: BC sends ``0001-01-01`` for an empty one (and a date there leaves no
    room for date arithmetic)."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    match = _YYYYMMDD.fullmatch(text) or _ISO.fullmatch(text)
    if match is None or int(match[1]) <= 1:
        return None
    if match.re is _YYYYMMDD:
        try:
            return datetime(int(match[1]), int(match[2]), int(match[3]), tzinfo=timezone.utc)
        except ValueError:
            return None
    try:
        moment = datetime(int(match[1]), int(match[2]), int(match[3]), int(match[4] or 0),
                          int(match[5] or 0), int(match[6] or 0),
                          int((match[7] or "0")[:6].ljust(6, "0")))
    except ValueError:
        return None
    zone = match[8]
    if zone in (None, "Z"):
        return moment.replace(tzinfo=timezone.utc)
    sign = 1 if zone[0] == "+" else -1
    digits = zone[1:].replace(":", "")
    offset = timedelta(hours=int(digits[:2]), minutes=int(digits[2:4]))
    try:
        return (moment - sign * offset).replace(tzinfo=timezone.utc)
    except OverflowError:   # past year 9999 in UTC
        return None


def field_value(row: Mapping[str, Any], field: str) -> Any:
    """``col`` or ``custom_fields.<key>`` (a JSON object column, object or JSON text)."""
    if "." not in field:
        return row.get(field)
    column, _, inner = field.partition(".")
    container = row.get(column)
    if isinstance(container, str):
        try:
            container = json.loads(container)
        except ValueError:
            return None
    return container.get(inner) if isinstance(container, Mapping) else None


# What a failed read may publish besides its error class: the HTTP status and the
# service's machine error code (integrations.http.SourceHttpError carries both).
_ERROR_CODE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,63}$")
# The runners' time-budget guard (scripts/eval_*.py ``BudgetExhausted``) stops a read; any
# other failed read is the source's own (``blocked: upstream``).
BUDGET_STOP = "BudgetExhausted"
# Why a read failed (``ReadFailed.cause``): the run's time budget stopped it (``budget``);
# BC refused a field filter as a bad request (``not_exposed``: the API v2.0 entity does not
# expose or filter that property); the BC query grammar refused the value before any
# request (``not_queryable``: no BC number literal, so no lookup was sent); otherwise the
# source failed (``upstream``). Only ``upstream`` and ``not_exposed`` are source failures.
FAILURE_CAUSES = ("budget", "not_exposed", "not_queryable", "upstream")
SOURCE_FAILURE_CAUSES = frozenset({"not_exposed", "upstream"})


def read_failure_label(reason: str, http_status: int | None, error_code: str | None,
                       cause: str) -> str:
    """The typed cause of a failed read as published, counts only: ``budget``,
    ``http_<status>``, ``error_<code>``, ``timeout`` (a timeout class) or else its cause
    (``upstream``). The one rule for relation reads and document searches alike."""
    if cause == "budget":
        return "budget"
    if http_status is not None:
        return f"http_{http_status}"
    if error_code is not None:
        return f"error_{error_code}"
    return "timeout" if "timeout" in reason.lower() else cause


class ReadFailed(RuntimeError):
    """A port read failed; ``reason`` is an error class name, never source text."""

    def __init__(self, target: str, reason: str, *, http_status: int | None = None,
                 error_code: str | None = None, cause: str | None = None) -> None:
        super().__init__(f"{target}: {reason}")
        self.target = target
        self.reason = reason
        self.http_status = http_status
        self.error_code = error_code
        self.cause = cause if cause in FAILURE_CAUSES else \
            "budget" if reason == BUDGET_STOP else "upstream"

    @classmethod
    def of(cls, target: str, exc: BaseException, *, field_read: bool = False
           ) -> "ReadFailed":
        """The failure of ``exc``: its class, and its HTTP status and machine error code
        when it carries them in shape (never its message). ``field_read``: a BC lookup by
        one field, whose HTTP 400 says the field is not exposed and whose ``ValueError``
        (raised by the query grammar before any request) says the value is not
        queryable (``cause``). A ``ReadFailed`` (a port serving another read's cache)
        keeps its own reason, status, code and cause."""
        if isinstance(exc, ReadFailed):
            return cls(target, exc.reason, http_status=exc.http_status,
                       error_code=exc.error_code, cause=exc.cause)
        # SourceHttpError carries ``status``, the BC/Graph SourceUnavailable ``http_status``
        try:
            status = exc.status
        except AttributeError:
            try:
                status = exc.http_status
            except AttributeError:
                status = None
        try:
            code = exc.error_code
        except AttributeError:
            code = None
        http_status = status if isinstance(status, int) and not isinstance(
            status, bool) and 100 <= status <= 599 else None
        cause = None
        if field_read and http_status == 400:
            cause = "not_exposed"
        elif field_read and isinstance(exc, ValueError):
            cause = "not_queryable"
        return cls(target, type(exc).__name__, http_status=http_status,
                   error_code=code if isinstance(code, str) and _ERROR_CODE.match(code)
                   else None, cause=cause)

    def again(self) -> "ReadFailed":
        """A fresh copy, so a cached failure is raised without growing its traceback."""
        return ReadFailed(self.target, self.reason, http_status=self.http_status,
                          error_code=self.error_code, cause=self.cause)

    def label(self) -> str:
        """The typed cause as published (``read_failure_label``)."""
        return read_failure_label(self.reason, self.http_status, self.error_code,
                                  self.cause)

    def fields(self) -> dict[str, Any]:
        """``blocked`` (``budget`` or ``upstream``) with the HTTP status and error code."""
        out: dict[str, Any] = {"blocked": "budget" if self.reason == BUDGET_STOP
                               else "upstream"}
        if self.http_status is not None:
            out["http_status"] = self.http_status
        if self.error_code is not None:
            out["error_code"] = self.error_code
        return out


class PortalReads:
    """Full reads of the served relations the oracle needs, cached for one run."""

    def __init__(self, read_relation: Callable[[str, Sequence[str]], Mapping[str, Any]]) -> None:
        self._read = read_relation
        self._reads: dict[str, Mapping[str, Any]] = {}
        self._failed: dict[str, ReadFailed] = {}
        self._index: dict[tuple[str, str], dict[str, list[dict[str, Any]]]] = {}
        self._generation: str | None = None

    def _load(self, relation: str) -> Mapping[str, Any]:
        if relation in self._failed:
            raise self._failed[relation].again()
        if relation not in self._reads:
            spec = RELATIONS[relation]
            try:
                result = self._read(relation, list(spec.columns))
                if not isinstance(result, Mapping) or not isinstance(result.get("rows"), list):
                    raise TypeError("read result shape")
            except Exception as exc:  # the port's own error types are not imported here
                self._failed[relation] = ReadFailed.of(relation, exc)
                raise self._failed[relation].again() from None
            self._reads[relation] = {**result, "rows": distinct(
                result["rows"], portal_keyed(result.get("key")))}
            self._generation = None
        return self._reads[relation]

    def read(self, relation: str, columns: Sequence[str] = ()) -> Mapping[str, Any]:
        """The cached read of ``relation`` in the port's result shape, so a read can serve
        another ``PortalReads`` as its port (``columns`` ignored: the oracle's own). Raises
        ``ReadFailed`` as the first read did."""
        return self._load(relation)

    def prefetch(self) -> None:
        """Read every relation the oracle uses once, so one run cites one generation."""
        for relation in RELATIONS:
            try:
                self._load(relation)
            except ReadFailed:
                pass

    def rows(self, relation: str) -> list[dict[str, Any]]:
        return list(self._load(relation)["rows"])

    def key_columns(self, relation: str) -> list[str]:
        key = self._load(relation).get("key")
        return list(key) if isinstance(key, list) and key else ["id"]

    def lookup(self, relation: str, field: str, value: Any) -> list[dict[str, Any]]:
        text = key_text(value)
        if text is None:
            return []
        index = self._index.get((relation, field))
        if index is None:
            index = {}
            for row in self._load(relation)["rows"]:
                found = key_text(field_value(row, field))
                if found is not None:
                    index.setdefault(found, []).append(row)
            self._index[(relation, field)] = index
        return list(index.get(text, []))

    def key_of(self, relation: str, row: Mapping[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for column in self.key_columns(relation):
            value = row.get(column)
            out[column] = value if isinstance(value, int) and not isinstance(value, bool) \
                else ("" if value is None else str(value))
        return out

    def failures(self) -> dict[str, str]:
        return {target: failed.reason for target, failed in self._failed.items()}

    def causes(self) -> dict[str, str]:
        """``ReadFailed.cause`` of every failed read, by target."""
        return {target: failed.cause for target, failed in self._failed.items()}

    def labels(self) -> dict[str, str]:
        """``ReadFailed.label`` of every failed read, by target."""
        return {target: failed.label() for target, failed in self._failed.items()}

    def failure(self, relation: str) -> dict[str, Any] | None:
        """``ReadFailed.fields`` of the failed read of ``relation`` (None: not failed)."""
        failed = self._failed.get(relation)
        return None if failed is None else failed.fields()

    def generation(self) -> str:
        """16-hex digest of every relation read so far (catalog revision + rows)."""
        if self._generation is None:
            parts = sorted((name, str(read.get("catalog_revision")), digest(read["rows"]))
                           for name, read in self._reads.items())
            self._generation = digest(parts)[:16]
        return self._generation

    def replica_time(self) -> str | None:
        """Earliest replica time reported by the reads (``health_before.generated_at``)."""
        times = []
        for read in self._reads.values():
            health = read.get("health_before")
            moment = instant(health.get("generated_at")) if isinstance(health, Mapping) else None
            if moment is not None:
                times.append(moment)
        if not times:
            return None
        return min(times).strftime("%Y-%m-%dT%H:%M:%SZ")


class BcPort(Protocol):
    """GET-only BC reads across the companies the caller may read. Records are
    ``{"company_id", "company_name", "entity_set", "id", "fields", "citation",
    "last_modified"}``; ``complete`` is false when a read was capped, withheld or failed.
    ``sample`` also says what it read (``read_state``: ``source``, ``withheld``,
    ``companies``)."""

    def find(self, entity_set: str, field: str, value: str) -> Mapping[str, Any]: ...

    def lines(self, record: Mapping[str, Any]) -> Mapping[str, Any]: ...

    def sample(self, entity_set: str, limit: int) -> Mapping[str, Any]: ...


def read_state(found: Mapping[str, Any]) -> dict[str, Any]:
    """What a sample read (``BcPort``/``DocumentPort`` ``sample``) says about itself: the
    sources it tried (``source``: entity sets, mailbox/folder, drive paths; never texts),
    ``not_configured`` (a reason) when the port has no reader for it, whether it was
    ``complete`` (absent = not complete), ``capped`` when only the caller's own sample
    limits cut it (a sample of a readable source, never a source that failed or withheld)
    and how many items the audience or ACL withheld."""
    state: dict[str, Any] = {"tried": [str(s) for s in found.get("source") or ()],
                             "complete": found.get("complete") is True,
                             "withheld": int(found.get("withheld") or 0)}
    if found.get("not_configured"):
        state["not_configured"] = str(found["not_configured"])
    if found.get("capped") is True and not state["complete"]:
        state["capped"] = True
    if isinstance(found.get("companies"), int):
        state["companies"] = found["companies"]
    return state


class BcReads:
    """Cached BC reads; exact (case-sensitive) equality on top of the port's filter. A
    failed find, lines or sample read is kept for its key too, so one run is one
    generation: a later ask of that key raises the same failure, never a fresh read."""

    def __init__(self, port: BcPort) -> None:
        self._port = port
        self._finds: dict[tuple[str, str, str], tuple[list[dict], bool]] = {}
        self._lines: dict[tuple[str, str], tuple[list[dict], bool]] = {}
        self._failed: dict[str, ReadFailed] = {}
        self._kept: dict[tuple[Any, ...], ReadFailed] = {}
        self._seen: dict[str, str] = {}
        self._sampled: dict[str, dict[str, Any]] = {}

    def _note(self, records: Iterable[Mapping[str, Any]]) -> None:
        for record in records:
            self._seen[str(record.get("citation"))] = digest(record.get("fields"))

    def _fail(self, key: tuple[Any, ...], target: str, failed: ReadFailed) -> ReadFailed:
        """Keep ``failed`` for the read ``key`` and record it under ``target``."""
        self._kept[key] = self._failed[target] = failed
        return failed.again()

    def find(self, entity_set: str, field: str, value: Any) -> tuple[list[dict], bool]:
        # Both sides trimmed, as Portal matches BC numbers (document-finance-queries.ts:326);
        # otherwise exact, case-sensitive text (BC itself compares Code fields ignoring case).
        text = sql_trim(key_text(value) or "")
        if not text:
            return [], True
        cache_key = (entity_set, field, text)
        if ("find", *cache_key) in self._kept:
            raise self._kept[("find", *cache_key)].again()
        if cache_key not in self._finds:
            try:
                found = self._port.find(entity_set, field, text)
                records = distinct(
                    (dict(r) for r in found.get("records", [])
                     if isinstance(r, Mapping) and isinstance(r.get("fields"), Mapping)
                     and sql_trim(key_text(r["fields"].get(field)) or "") == text), bc_keyed)
                complete = bool(found.get("complete"))
            except Exception as exc:
                raise self._fail(("find", *cache_key), f"bc:{entity_set}.{field}",
                                 ReadFailed.of(f"bc:{entity_set}", exc, field_read=True)
                                 ) from None
            self._note(records)
            self._finds[cache_key] = (records, complete)
        records, complete = self._finds[cache_key]
        return [dict(r) for r in records], complete

    def lines(self, record: Mapping[str, Any]) -> tuple[list[dict], bool]:
        cache_key = (str(record.get("company_id")), str(record.get("id")))
        if ("lines", *cache_key) in self._kept:
            raise self._kept[("lines", *cache_key)].again()
        if cache_key not in self._lines:
            try:
                found = self._port.lines(record)
                records = distinct((dict(r) for r in found.get("records", [])
                                    if isinstance(r, Mapping)
                                    and isinstance(r.get("fields"), Mapping)), bc_keyed)
                complete = bool(found.get("complete"))
            except Exception as exc:
                target = f"bc:{record.get('entity_set')}.lines"
                raise self._fail(("lines", *cache_key), target,
                                 ReadFailed.of(target, exc)) from None
            self._note(records)
            self._lines[cache_key] = (records, complete)
        records, complete = self._lines[cache_key]
        return [dict(r) for r in records], complete

    def sample(self, entity_set: str, limit: int) -> list[dict]:
        key = ("sample", entity_set, limit)
        if key in self._kept:
            raise self._kept[key].again()
        try:
            found = self._port.sample(entity_set, limit)
        except Exception as exc:
            raise self._fail(key, f"bc:{entity_set}.sample",
                             ReadFailed.of(f"bc:{entity_set}", exc)) from None
        records = [dict(r) for r in found.get("records", []) if isinstance(r, Mapping)
                   and isinstance(r.get("fields"), Mapping)]
        self._note(records)
        self._sampled[entity_set] = read_state(found)
        return records

    def sample_state(self, entity_set: str) -> dict[str, Any] | None:
        """``read_state`` of the last ``sample`` of ``entity_set`` (None before one)."""
        state = self._sampled.get(entity_set)
        return None if state is None else dict(state)

    def failures(self) -> dict[str, str]:
        return {target: failed.reason for target, failed in self._failed.items()}

    def causes(self) -> dict[str, str]:
        """``ReadFailed.cause`` of every failed read, by target."""
        return {target: failed.cause for target, failed in self._failed.items()}

    def labels(self) -> dict[str, str]:
        """``ReadFailed.label`` of every failed read, by target."""
        return {target: failed.label() for target, failed in self._failed.items()}

    def generation(self) -> str | None:
        if not self._seen:
            return None
        return digest(sorted(self._seen.items()))[:16]


class DocumentPort(Protocol):
    """Mail/SharePoint documents for the generator's document strata:
    ``{"records": [{"handle": "graph-mail://..."|"sharepoint://...", "texts": [...]}],
    "complete": bool, "capped": bool, "withheld": int, "source": [...],
    "not_configured": reason}`` (``read_state``; ``capped``: only the sampler's own limits,
    which grow with ``limit``, left it incomplete). Texts are subjects, previews or file names;
    a record's ``contents`` hold text read from the document itself (``texts_read`` and
    ``texts_unread`` count those reads, ``texts_unread_by`` the unread by cause); never
    stored. ``windows`` (UTC ISO instants): a time-ordered source reads oldest first from
    each and says how many it read
    (``windows``); ``seed`` ranks what a bounded read picks. A port may ignore both."""

    def sample(self, kind: str, limit: int, *, windows: Sequence[str] = (),
               seed: str = "") -> Mapping[str, Any]: ...

    def search(self, kind: str, queries: Sequence[str], *, hits: int,
               seed: str = "") -> Mapping[str, Any]:
        """GET-only search of ``kind`` for each query (one number as written), at most
        ``hits`` documents each, every hit checked for visibility by the port's reader:
        ``{"results": [{"query", "handles": [...], "complete": bool, "withheld": int} |
        {"query", "refused": cause} | {"query", "failed": cause}], "records": [{"handle",
        "texts", "contents"}], "source": [...], "not_configured": reason}`` with the text
        read counts of ``sample``. ``refused``: ``guard`` (the access guard, the reader's
        audience or the query grammar refused it before any I/O) or ``tenant`` (the source
        refused the search); ``failed``: the search read failed. A record is listed once,
        however many queries found it; ``seed`` ranks which hits' texts are read. A port may
    say how many roots one run searches it for (attribute ``max_roots``; else the
    generator's ``MAX_ROOT_SEARCHES``)."""
        ...

    def list_documents(self, kind: str, limit: int, *, seed: str = "") -> Mapping[str, Any] | None:
        """Optional: the listing read of ``kind`` the generator names when its searches
        named no root, at most ``limit`` documents (``generator.MAX_LISTED_DOCUMENTS``), in
        ``sample``'s shape; a document the run's searches found keeps their read, so each
        text is read and counted once a run. None: ``kind`` is not listed."""
        ...


__all__ = ["BcPort", "BcReads", "DocumentPort", "FAILURE_CAUSES", "PortalReads", "ReadFailed",
           "SOURCE_FAILURE_CAUSES", "canonical", "decimal_of", "decimal_text", "digest",
           "bc_keyed", "distinct", "field_value", "instant", "key_text", "portal_keyed",
           "read_state", "sql_trim"]
