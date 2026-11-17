"""The item gate's decisions as indexed principal keys on Postgres (AUD-01, migration 0027).

The read tools gate every handle of an item-ACL scheme (``ontology_tools._ITEM_ACL_SCHEMES``:
graph-mail, sharepoint, sharepoint-list, original, teams-channel, teams-chat,
calendar-event, planner-task, meeting-transcript) through ``OntologyTools._item_allowed``:
the host's gate over the case runtime's ``resolve_citation`` (mailbox scope and owner
ACL, SharePoint item permissions and file class, Teams and list memberships, original
audiences, the people exclusion, the capture-age bound). On Postgres that gate ran in
rounds (``sqlite_compat.MAX_ROUNDS``): each statement ran again for the handles Python had
not yet answered.

Here a handle is decided ahead, once per reader of a roster, by that very gate
(``decide``: ``base._item_allowed(actor, handle)``, the call the traverse tools' SQL gate
makes), so a recorded decision cannot differ from the gate's at the time it was made:

* Reader keys (``reader_key``): a digest of the host's gate scope
  (``OntologyTools.item_gate_scope``), the gate's own key material for the actor
  (``ontology_tools.runtime_key_material``: the actor, its employee entitlement's
  fingerprint, the people exclusion's list) and ``PLX_ITEM_AUDIENCE_GENERATION``. Any
  change in access, in the exclusion or in the host gives the reader a key no recorded
  roster holds, so every handle falls back to the gate. No key at all (None) for a reader
  the exclusion names, or cannot decide, or whose host states no material.
* A decision is kept (``Decision.kept``) only when it is the gate's for as long as it is
  read: every capture-age check the gate made (``capture_age.DECISIONS``) passed, and it
  read no live source (``live_source_guard.CAPTURES_ONLY``: a live request is refused and
  noted, so recording never calls a business system). ``valid_until`` is when the first
  permission stamp the gate read passes its bound; ``captured_at`` the oldest stamp.
  Anything else (a stale or undated list, a live read, no capture-age check at all, a
  scheme outside ``RECORDABLE_SCHEMES``) keeps no row: the gate decides it as before.
* Writes are set-based (``write``): the roster's keys, then per handle its allowed keys and
  state in one transaction, an unchanged decision (same ``acl_digest``) left alone.
* Invalidation (``invalidate``, ``invalidate_staged``): the projection's delta drops the
  state of every handle of an object whose versions it changes, in its own transaction.

Read side (``sqlite_compat``): a gate function carrying ``item_keys`` is called as
``ontology.sq_item_gate``, which answers from these rows only for a handle whose state row
is of a roster holding the reader's key and still valid, else exactly as ``sq_call``.

Recordable schemes (``RECORDABLE_SCHEMES``; ``REFUSED_SCHEMES`` says why each other one is
not): only those whose permission change makes a new captured version and whose decision a
capture-age check bounds. ``decide``, ``write`` and the backfill refuse the rest.

Backfill (``python3 -m operating.pg.item_audiences backfill``): per scheme, partitioned by a
hash of the handle over at most 4 worker processes, a 10% rehearsal with ``--sample 10``,
a time budget, dry run unless ``--apply``.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
import sys
import time
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

KEY_VERSION = "item-audiences/1"
# The schemes whose gate decisions may be recorded: a permission change of the item makes a
# new captured version (dropped by the projection delta, ``invalidate_staged``) and the
# gate's decision is bounded by a capture-age check (``valid_until``). Every other scheme is
# decided by the gate on every read, because capture changes what decides it in place,
# where no new version and no reader key shows it (decided from code, 11-14):
REFUSED_SCHEMES = {
    "graph-mail": "mailbox owner ACL, identities and the directory the exclusion reads are "
                  "rewritten in place (capture_mail record_mailbox, record_directory); "
                  "no capture-age check",
    "sharepoint": "drive item source_acl rewritten in place (capture_sharepoint UPDATE "
                  "items SET source_acl)",
    "original": "decided by the recorded or live source item (SharePoint drive item, Teams "
                "chat, mailbox), each changed in place, or by a live re-read",
    "teams-channel": "unit membership rewritten in place (CaptureStore.set_unit_acl); people "
                     "exclusion over addresses the captured directory maps",
    "teams-chat": "unit membership rewritten in place (CaptureStore.set_unit_acl); people "
                  "exclusion over addresses the captured directory maps",
    "calendar-event": "no item permission version and no capture-age check; people exclusion "
                      "over addresses the captured directory maps",
    "planner-task": "people exclusion over addresses the captured directory or tenant "
                    "listing maps, both changed in place",
    "meeting-transcript": "people exclusion over addresses the captured directory or tenant "
                          "listing maps, both changed in place",
}
RECORDABLE_SCHEMES = frozenset({"sharepoint-list"})


def recordable(handle: Any) -> bool:
    """Whether a decision of ``handle`` may be recorded (``RECORDABLE_SCHEMES``)."""
    return isinstance(handle, str) and handle.partition("://")[0] in RECORDABLE_SCHEMES \
        and "://" in handle
GENERATION_ENV = "PLX_ITEM_AUDIENCE_GENERATION"
MAX_WORKERS = 4
BATCH = 2000
BUDGET_S = 7200


def reader_key(base: Any, actor: Any, environ: Mapping[str, str] | None = None
               ) -> str | None:
    """``actor``'s audience key under ``base``'s item gate, or None (module docstring)."""
    scope = getattr(base, "item_gate_scope", None)
    material_of = getattr(getattr(base, "item_gate", None), "key_material", None)
    if scope is None or not callable(material_of):
        return None
    try:
        material = material_of(actor)
    except Exception:  # noqa: BLE001 - no material: no key
        return None
    if material is None:
        return None
    environ = os.environ if environ is None else environ
    body = {"v": KEY_VERSION, "generation": environ.get(GENERATION_ENV, ""),
            "scope": [str(part) for part in scope], "material": material}
    try:
        text = json.dumps(body, sort_keys=True)
    except (TypeError, ValueError):
        return None
    return "r:" + hashlib.sha256(text.encode()).hexdigest()


def roster_id(keys: Iterable[str]) -> str:
    return "ro:" + hashlib.sha256(json.dumps(sorted(set(keys))).encode()).hexdigest()[:40]


@dataclass
class _Record:
    stamps: list
    live: list


@contextmanager
def recording() -> Iterator[_Record]:
    """Within: every capture-age check is noted and every live request noted and refused."""
    from integrations.live_source_guard import CAPTURES_ONLY
    from operating.capture_age import DECISIONS
    record = _Record([], [])
    stamps, live = DECISIONS.set(record.stamps), CAPTURES_ONLY.set(record.live)
    try:
        yield record
    finally:
        CAPTURES_ONLY.reset(live)
        DECISIONS.reset(stamps)


@dataclass(frozen=True)
class Decision:
    handle: str
    keys: tuple[str, ...]          # the roster readers the gate allows
    kept: bool                     # recorded (else the handle falls back to the gate)
    captured_at: datetime | None
    valid_until: datetime | None
    digest: str


def decide(handle: str, readers: Sequence[tuple[str, Callable[[str], Any]]],
           roster: str) -> Decision:
    """Decide ``handle`` for each (key, allows) reader through its own gate call."""
    allowed: list[str] = []
    stamps: list = []
    live = False
    failed = False
    for key, allows in readers:
        with recording() as record:
            try:
                ok = allows(handle) is True
            except Exception:  # noqa: BLE001 - a failing gate denies, and is not kept
                ok, failed = False, True
        stamps.extend(record.stamps)
        live = live or bool(record.live)
        if ok:
            allowed.append(key)
    dated = [(moment, age) for moment, age, _answer in stamps]
    # kept only with an end: a decision no capture-age check bounds is never recorded
    kept = (not failed and not live and bool(dated)
            and all(answer for _m, _a, answer in stamps)
            and all(moment is not None for moment, _ in dated) and recordable(handle))
    captured = min((m for m, _ in dated), default=None) if kept else None
    until = min((m + a for m, a in dated), default=None) if kept else None
    keys = tuple(sorted(set(allowed)))
    digest = hashlib.sha256(json.dumps(
        [roster, keys, None if captured is None else captured.isoformat(),
         None if until is None else until.isoformat()]).encode()).hexdigest()
    return Decision(handle, keys, kept, captured, until, digest)


def write(conn: Any, roster_keys: Sequence[str], decisions: Sequence[Decision]
          ) -> dict[str, int]:
    """Record ``decisions`` of the roster ``roster_keys`` (one transaction, set-based). A
    decision not kept drops the handle's state (it falls back to the gate)."""
    roster = roster_id(roster_keys)
    report = {"kept": 0, "dropped": 0, "unchanged": 0, "keys": 0}
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("CREATE TEMP TABLE IF NOT EXISTS ia_roster (audience_key text)")
        cur.execute("CREATE TEMP TABLE IF NOT EXISTS ia_dec (l1_handle text PRIMARY KEY, "
                    "acl_digest text, captured_at timestamptz, valid_until timestamptz, "
                    "kept boolean)")
        cur.execute("CREATE TEMP TABLE IF NOT EXISTS ia_keys (l1_handle text, "
                    "audience_key text)")
        cur.execute("TRUNCATE ia_roster, ia_dec, ia_keys")
        with cur.copy("COPY ia_roster (audience_key) FROM STDIN") as copy:
            for key in sorted(set(roster_keys)):
                copy.write_row((key,))
        cur.execute("INSERT INTO ontology.item_audience_rosters (roster, audience_key) "
                    "SELECT %s, audience_key FROM ia_roster ON CONFLICT DO NOTHING", (roster,))
        # a decision of a scheme that may not be recorded, or with no end, only drops
        latest = {d.handle: d if d.kept and recordable(d.handle) and d.valid_until is not None
                  else Decision(d.handle, (), False, None, None, d.digest)
                  for d in decisions}
        with cur.copy("COPY ia_dec FROM STDIN") as copy:
            for d in latest.values():
                copy.write_row((d.handle, d.digest, d.captured_at, d.valid_until, d.kept))
        with cur.copy("COPY ia_keys FROM STDIN") as copy:
            for d in latest.values():
                if d.kept:
                    for key in d.keys:
                        copy.write_row((d.handle, key))
        cur.execute("DELETE FROM ia_dec d USING ontology.item_acl_state s WHERE d.kept "
                    "AND s.l1_handle = d.l1_handle AND s.acl_digest = d.acl_digest "
                    "AND s.roster = %s", (roster,))
        report["unchanged"] = cur.rowcount
        cur.execute("DELETE FROM ontology.item_acl_state s USING ia_dec d "
                    "WHERE s.l1_handle = d.l1_handle")
        cur.execute("DELETE FROM ontology.item_audiences a USING ia_dec d "
                    "WHERE a.l1_handle = d.l1_handle")
        cur.execute("INSERT INTO ontology.item_audiences (l1_handle, audience_key) "
                    "SELECT k.l1_handle, k.audience_key FROM ia_keys k JOIN ia_dec d "
                    "ON d.l1_handle = k.l1_handle WHERE d.kept ON CONFLICT DO NOTHING")
        report["keys"] = cur.rowcount
        cur.execute("INSERT INTO ontology.item_acl_state (l1_handle, acl_digest, captured_at, "
                    "valid_until, roster) SELECT l1_handle, acl_digest, captured_at, "
                    "valid_until, %s FROM ia_dec WHERE kept", (roster,))
        report["kept"] = cur.rowcount
        cur.execute("SELECT count(*) FROM ia_dec WHERE NOT kept")
        report["dropped"] = cur.fetchone()[0]
    return report


def invalidate(conn: Any, handles: Iterable[str]) -> int:
    """Drop the recorded decisions of ``handles``: the gate decides them again."""
    listed = sorted({h for h in handles if isinstance(h, str)})
    if not listed:
        return 0
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("DELETE FROM ontology.item_acl_state WHERE l1_handle = ANY(%s::text[])",
                    (listed,))
        dropped = cur.rowcount
        cur.execute("DELETE FROM ontology.item_audiences WHERE l1_handle = ANY(%s::text[])",
                    (listed,))
    return dropped


def invalidate_staged(cur: Any) -> int:
    """In the projection delta's transaction (``ontology_delta._diff_versions``, after its
    version match): drop the state of every handle, old or new, of an object whose
    versions the delta changes (a version added, removed, or superseded since). A later
    version changes an earlier one's gate (its permissions are the item's latest), so the
    whole object is decided again. Nothing when migration 0027 is not applied."""
    cur.execute("SELECT to_regclass('ontology.item_acl_state') IS NOT NULL")
    if not cur.fetchone()[0]:
        return 0
    cur.execute(
        "WITH changed AS ("
        " SELECT l.object_id FROM diff_lv l WHERE NOT EXISTS (SELECT 1 FROM diff_vmatch m "
        "  WHERE m.lver = l.lver)"
        " UNION SELECT p.object_id FROM diff_pv p WHERE NOT EXISTS (SELECT 1 FROM diff_vmatch "
        "  m WHERE m.ver = p.ver)"
        " UNION SELECT v.object_id FROM ontology.object_versions v JOIN diff_vmatch m "
        "  ON m.ver = v.ver WHERE v.superseded_at IS DISTINCT FROM m.superseded_at)"
        "DELETE FROM ontology.item_acl_state s WHERE s.l1_handle IN ("
        " SELECT l.l1_handle FROM diff_lv l JOIN changed c ON c.object_id = l.object_id"
        " UNION SELECT p.l1_handle FROM diff_pv p JOIN changed c ON c.object_id = p.object_id)")
    return cur.rowcount


# --------------------------------------------------------------------------- backfill

def _partition_sql(workers: int, sample: int) -> str:
    # a handle's partition and its rehearsal sample from one hash, so a rehearsal's handles
    # are the same ones the full run decides in the same worker
    sampled = "" if sample >= 100 else (
        f" AND mod(abs(hashtext(l1_handle)::bigint) / {workers}, 100) < {sample}")
    return ("SELECT DISTINCT l1_handle FROM ontology.object_versions "
            "WHERE l1_handle LIKE %(like)s AND mod(abs(hashtext(l1_handle)::bigint), "
            f"{workers}) = %(part)s{sampled}")


def _readers_from(path: str) -> list[Any]:
    from ontology.types import Actor
    listed = json.loads(open(path, encoding="utf-8").read())
    return [Actor(r["tenant"], frozenset(r["principals"]), r.get("clearance", "internal"))
            for r in listed]


def _worker(args: tuple) -> dict[str, Any]:
    (dsn, state_dir, config, readers_file, scheme, part, workers, sample, apply,
     deadline) = args
    _require_recordable(scheme)
    import psycopg
    from operating.host import Host, HostConfig
    host = Host(state_dir, HostConfig.from_file(config))
    report: dict[str, Any] = {"part": part, "decided": 0, "kept": 0, "allowed_pairs": 0,
                              "fallback": 0, "complete": False, "readers_keyed": 0}
    with ExitStack() as stack:
        readers: list[tuple[str, Callable[[str], Any]]] = []
        for actor in _readers_from(readers_file):
            served = stack.enter_context(host.served_tools(actor))
            base = served.tools
            key = reader_key(base, actor)
            if key is None:
                continue   # a reader with no key is never recorded for
            readers.append((key, lambda h, base=base, actor=actor: base._item_allowed(actor, h)))
        report["readers_keyed"] = len(readers)
        if not readers:
            report["complete"] = True
            return report
        keys = [k for k, _ in readers]
        roster = roster_id(keys)
        with psycopg.connect(dsn) as read, psycopg.connect(dsn, autocommit=True) as out:
            with read.cursor(name="item_audience_handles") as cur:
                cur.itersize = BATCH
                cur.execute(_partition_sql(workers, sample),
                            {"like": scheme.replace("_", r"\_") + "://%", "part": part})
                batch: list[Decision] = []
                for (handle,) in cur:
                    if time.monotonic() > deadline:
                        break
                    decision = decide(handle, readers, roster)
                    report["decided"] += 1
                    report["kept" if decision.kept else "fallback"] += 1
                    report["allowed_pairs"] += len(decision.keys)
                    batch.append(decision)
                    if len(batch) >= BATCH:
                        if apply:
                            write(out, keys, batch)
                        batch = []
                else:
                    report["complete"] = True
                if batch and apply:
                    write(out, keys, batch)
    return report


def _require_recordable(scheme: str) -> None:
    if scheme not in RECORDABLE_SCHEMES:
        why = REFUSED_SCHEMES.get(scheme, "not an item-ACL scheme")
        raise ValueError(f"scheme_not_recordable:{scheme}: {why}")


def backfill(dsn: str, state_dir: str, config: str, readers_file: str, scheme: str, *,
             workers: int = MAX_WORKERS, sample: int = 100, apply: bool = False,
             budget_s: float = BUDGET_S) -> dict[str, Any]:
    """One scheme's handles decided for the listed readers, ``workers`` processes (at most
    4), each its hash partition; ``sample`` percent of each (10: the rehearsal). Dry run
    (decides, writes nothing) unless ``apply``. Stops at ``budget_s`` (reported
    incomplete; a run again redoes only changed decisions)."""
    _require_recordable(scheme)
    import multiprocessing
    workers = max(1, min(int(workers), MAX_WORKERS))
    sample = max(1, min(int(sample), 100))
    deadline = time.monotonic() + budget_s
    jobs = [(dsn, state_dir, config, readers_file, scheme, part, workers, sample, apply,
             deadline) for part in range(workers)]
    started = time.monotonic()
    from ontology import parent_watch
    with multiprocessing.get_context("spawn").Pool(
            workers, **parent_watch.pool_init()) as pool:
        parts = pool.map(_worker, jobs)
    total = {k: sum(p[k] for p in parts) for k in ("decided", "kept", "fallback",
                                                     "allowed_pairs")}
    return {"scheme": scheme, "applied": apply, "sample_percent": sample, "workers": workers,
            "seconds": round(time.monotonic() - started, 1),
            "complete": all(p["complete"] for p in parts),
            "readers_keyed": min((p["readers_keyed"] for p in parts), default=0),
            **total}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m operating.pg.item_audiences")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("backfill", help="decide one scheme's handles for a reader roster")
    run.add_argument("--state-dir", required=True)
    run.add_argument("--config", required=True, help="the served host's config file")
    run.add_argument("--readers", required=True,
                     help="JSON list of {tenant, principals, clearance}")
    run.add_argument("--scheme", required=True, choices=sorted(RECORDABLE_SCHEMES))
    run.add_argument("--workers", type=int, default=MAX_WORKERS)
    run.add_argument("--sample", type=int, default=100, help="percent (10: rehearsal)")
    run.add_argument("--budget-s", type=float, default=BUDGET_S)
    run.add_argument("--apply", action="store_true", help="write (default: dry run)")
    args = parser.parse_args(argv)
    from operating.pg.config import dsn
    report = backfill(dsn(), args.state_dir, args.config, args.readers, args.scheme,
                      workers=args.workers, sample=args.sample, apply=args.apply,
                      budget_s=args.budget_s)
    print(json.dumps(report, sort_keys=True))
    return 0 if report["complete"] else 3


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["RECORDABLE_SCHEMES", "REFUSED_SCHEMES", "recordable", "Decision", "backfill", "decide", "invalidate", "invalidate_staged",
           "reader_key", "recording", "roster_id", "write"]
