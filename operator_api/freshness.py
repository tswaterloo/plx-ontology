"""Source freshness of the app API: capture alarms, delta-layer freshness, build waits,
documents pending, excluded records, search coverage and the data cut-off.

Moved from ``operator_api.backend`` by concern (no behaviour change); every name is
re-exported there."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Mapping, Optional, Sequence

from . import models as m
from .serving import BEHIND_SECONDS


def _instant(value: Optional[str]) -> Optional[datetime]:
    """An ISO capture time as an aware instant (naive read as UTC); None when unreadable."""
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def capture_alarm_status(state_dir: Any, now: Optional[datetime] = None
                         ) -> Optional[m.CaptureAlarm]:
    """The stale-capture alarm over the served state dir (``operating.ops_health``'s capture
    and start checks); None with no state dir, nothing stalled, or an unreadable ledger."""
    if not state_dir:
        return None
    from operating import ops_health
    try:
        state = Path(state_dir)
        now = now or datetime.now(timezone.utc)
        t = ops_health.targets()
        checks = (ops_health.capture_checks(state, now, t)
                  + ops_health.source_checks(state, now, t))
        extra = [c for c in (ops_health.start_check(state, t),
                             ops_health.service_check(state, now),
                             ops_health.config_keys_check(state)) if c is not None]
        alarm = ops_health.capture_alarm({"checks": checks + extra})
    except Exception:  # noqa: BLE001 - a health line never breaks a page
        return None
    return None if alarm is None else m.CaptureAlarm(**alarm)


def _freshness_entries(raw: Any) -> Optional[list[dict[str, Any]]]:
    """A store's ``freshness`` meta as entries with a ``source`` (None when unreadable)."""
    if isinstance(raw, dict):
        raw = [dict(v, source=k) for k, v in raw.items() if isinstance(v, dict)]
    return [e for e in raw if isinstance(e, dict) and isinstance(e.get("source"), str)] \
        if isinstance(raw, list) else None


def with_delta_freshness(entries: list[dict[str, Any]], store: Path,
                         digest: Optional[str]) -> list[dict[str, Any]]:
    """``entries`` with the capture time the row-delta overlay serves for the sources it
    reads (its newest layer's ``delta_families``) when that is newer: answers read those
    records through the overlay (``operating.delta_overlay``), so the data is that fresh.
    Only a pointer built over the served generation (``digest``) counts; any other,
    missing or unreadable one leaves ``entries`` as they are."""
    try:
        from operating import delta_overlay
        pointer = delta_overlay.read_pointer(delta_overlay.pointer_path(store))
        if not pointer or not digest or pointer.get("base_digest") != digest:
            return entries
        conn = sqlite3.connect(f"file:{pointer['store']}?mode=ro", uri=True, timeout=1)
        try:
            meta = dict(conn.execute("SELECT key, value FROM store_meta WHERE key IN "
                                     "('freshness', 'delta_families')").fetchall())
        finally:
            conn.close()
        families = set(json.loads(meta.get("delta_families") or "[]"))
        layer = {e["source"]: e for e in _freshness_entries(
            json.loads(meta.get("freshness") or "null")) or [] if e["source"] in families}
    except (sqlite3.Error, ValueError, TypeError, OSError, KeyError):
        return entries
    out = []
    for entry in entries:
        newer = layer.get(entry["source"])
        if newer and isinstance(newer.get("captured_at"), str) and (
                not isinstance(entry.get("captured_at"), str)
                or newer["captured_at"] > entry["captured_at"]):
            entry = dict(entry, captured_at=newer["captured_at"],
                         completeness=newer.get("completeness", entry.get("completeness")))
        out.append(entry)
    return out


def _gigabytes(count: int) -> str:
    return f"{count / 1e9:,.0f} GB"


def build_wait_status(state_dir: Optional[Path]) -> Optional[m.BuildWait]:
    """The next projection build's wait for disk space (``operating.host.build_wait``),
    None when none waits or the space it waits for is free now (it builds at the host's
    next check)."""
    if state_dir is None:
        return None
    try:
        from operating.host import build_wait
        wait = build_wait(state_dir)
    except Exception:  # noqa: BLE001 - nothing is said about the build
        return None
    if not wait:
        return None
    try:
        import shutil
        free: Optional[int] = shutil.disk_usage(state_dir).free
    except OSError:
        free = wait.get("free_bytes") if isinstance(wait.get("free_bytes"), int) else None
    if free is not None and free >= wait["needed_bytes"]:
        return None
    mode = wait.get("mode") if wait.get("mode") in ("cold", "incremental") else None
    return m.BuildWait(needed_bytes=wait["needed_bytes"], free_bytes=free,
                       checked_at=wait.get("checked_at"), mode=mode)


def _record_key(key: Any) -> str:
    return json.dumps(list(key) if isinstance(key, (list, tuple)) else key, sort_keys=True)


def documents_pending(store: Path, digest: Optional[str], handles: Iterable[str] = (), *,
                      state_dir: Optional[Path] = None,
                      environ: Optional[Mapping[str, str]] = None
                      ) -> Optional[m.DocumentsPending]:
    """The documents the newest delta layer over the served generation (``digest``) left
    pending, counted without the excluded people's mail and OneDrives
    (``excluded_records``): ``records``, the records holding them (the layer's
    ``documents_pending`` meta) the exclusion clears; ``files`` and ``timed_out``, the
    pointer's counts, only while no pending record is withheld (the layer does not say
    which files a record holds). For ``handles`` (a record page's own sources, which its
    reader sees), whether a record they cite is one holding them. Never a record key: a
    reader learns only about the record in front of them. None when nothing (cleared) is
    pending or the delta cannot say."""
    try:
        from operating import delta_overlay
        pointer = delta_overlay.read_pointer(delta_overlay.pointer_path(store))
    except Exception:  # noqa: BLE001 - nothing is said about documents
        return None
    if not pointer or not digest or pointer.get("base_digest") != digest:
        return None
    counts = pointer.get("documents")
    if not isinstance(counts, Mapping) or not isinstance(counts.get("pending"), int) \
            or counts["pending"] <= 0:
        return None
    try:
        conn = sqlite3.connect(f"file:{pointer['store']}?mode=ro", uri=True, timeout=1)
        try:
            row = conn.execute("SELECT value FROM store_meta WHERE key = "
                               "'documents_pending'").fetchone()
        finally:
            conn.close()
        records = (json.loads(row[0]) or {}).get("records") if row else {}
        records = {source: [k for k in keys or ()] for source, keys in (records or {}).items()
                   if isinstance(source, str)}
    except (sqlite3.Error, ValueError, TypeError, AttributeError, KeyError):
        return None
    withheld = excluded_records(records, state_dir,
                                os.environ if environ is None else environ)
    kept = {(source, _record_key(key)) for source, keys in records.items() for key in keys
            if (source, _record_key(key)) not in withheld}
    if withheld and not kept:
        return None   # all of it is the excluded people's: nothing is said
    this = None
    handles = [h for h in handles if isinstance(h, str)]
    if handles:
        this = False
        try:
            from ontology.delta import record_of_handle
            for handle in handles:
                hit = record_of_handle(handle)
                if hit is not None and (hit[0], _record_key(hit[1])) in kept:
                    this = True
                    break
        except (ValueError, TypeError, AttributeError):
            this = None
    whole = not withheld
    return m.DocumentsPending(
        files=counts["pending"] if whole else None,
        timed_out=(counts.get("timed_out") if isinstance(counts.get("timed_out"), int)
                   else 0) if whole else None,
        records=len(kept), this_record=this)


def excluded_records(records: Mapping[str, Sequence[Any]], state_dir: Optional[Path],
                     environ: Mapping[str, str]) -> set[tuple[str, str]]:
    """The (source, record key) pairs of ``records`` that are an excluded person's: a mail
    message of a mailbox the people exclusion does not clear (``Exclusion.identity_clear``
    through the mail capture's directory), a SharePoint item of a OneDrive it does not
    clear or whose drive the capture does not place. Undecided withholds."""
    mail = [k for k in records.get("graph_mail") or () if isinstance(k, list) and k]
    items = [k for k in records.get("sharepoint") or () if isinstance(k, list) and len(k) > 1]
    if not mail and not items:
        return set()
    from integrations.people_exclusion import PeopleExclusion
    from operating.chunking.permissions import Exclusion, onedrive_segment
    directory: dict[str, str] = {}
    domains: set[str] = set()
    owners: dict[str, Optional[str]] = {}
    state = Path(state_dir) if state_dir is not None else None
    try:
        if state is not None and mail and (state / "mail-capture" / "capture.db").is_file():
            conn = sqlite3.connect(f"file:{state / 'mail-capture' / 'capture.db'}?mode=ro",
                                   uri=True, timeout=1)
            try:
                for mailbox, upn, identities in conn.execute(
                        "SELECT mailbox_id, upn, identities FROM mailboxes"):
                    try:
                        found = [upn, *json.loads(identities or "[]")]
                    except ValueError:
                        found = [upn]
                    for address in found:
                        if isinstance(address, str) and "@" in address:
                            address = address.split(":", 1)[-1].lower()
                            directory[address] = str(mailbox)
                            domains.add(address.rsplit("@", 1)[1])
            finally:
                conn.close()
        path = state / "sharepoint-capture" / "capture.db" if state is not None else None
        if items and path is not None and path.is_file():
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1)
            try:
                for drive in {str(k[1]) for k in items}:
                    row = conn.execute("SELECT json_extract(record, '$.original_url') FROM "
                                       "items WHERE drive_id = ? LIMIT 1", (drive,)).fetchone()
                    if row is not None:
                        owners[drive] = onedrive_segment(row[0])
            finally:
                conn.close()
    except sqlite3.Error:
        directory, owners = {}, {}
    exclusion = Exclusion(PeopleExclusion.from_env(environ), directory=directory,
                          internal_domains=domains, drive_owners=owners)
    out: set[tuple[str, str]] = set()
    for key in mail:
        if not exclusion.identity_clear(str(key[0])):
            out.add(("graph_mail", _record_key(key)))
    for key in items:
        segment = owners.get(str(key[1]), None)
        if str(key[1]) not in owners or segment is None \
                or not exclusion.onedrive_clear(segment):
            out.add(("sharepoint", _record_key(key)))
    return out


def with_search_coverage(sources: Sequence[m.FreshnessSource]) -> list[m.FreshnessSource]:
    """``sources`` with each one's ``searchable_to`` (the newest text hybrid search can
    find, ``operating.search.coverage``) when hybrid search is on; unchanged otherwise or
    when the index cannot be read."""
    try:
        from operating.search import coverage, plug
        if not plug.enabled():
            return list(sources)
        found = coverage.coverage()
    except Exception:  # noqa: BLE001 - nothing is claimed about search
        return list(sources)
    return [s.model_copy(update={"searchable_to": found[s.system]})
            if s.system in found else s for s in sources]


def search_lagging(sources: Sequence[m.FreshnessSource]) -> list[m.FreshnessSource]:
    """Sources whose searchable text ends over ``BEHIND_SECONDS`` before their capture,
    oldest first: captured records after it exist but search cannot find them yet."""
    out = []
    for s in sources:
        captured, searchable = _instant(s.as_of), _instant(s.searchable_to)
        if captured and searchable and (captured - searchable).total_seconds() > BEHIND_SECONDS:
            out.append((searchable, s))
    return [s for _at, s in sorted(out, key=lambda pair: pair[0])]


def data_cut_off(sources: Sequence[m.FreshnessSource]
                 ) -> tuple[Optional[str], list[m.FreshnessSource]]:
    """The served data's cut-off (its newest source capture, the moment the build pinned
    its sources) and the sources read more than ``BEHIND_SECONDS`` before it, oldest
    first. Compared as instants, so two spellings of one moment agree."""
    timed = [(at, s) for s in sources if (at := _instant(s.as_of)) is not None]
    if not timed:
        return None, []
    newest_at, newest = max(timed, key=lambda pair: pair[0])
    behind = sorted(((at, s) for at, s in timed
                     if (newest_at - at).total_seconds() > BEHIND_SECONDS),
                    key=lambda pair: pair[0])
    return newest.as_of, [s for _at, s in behind]
