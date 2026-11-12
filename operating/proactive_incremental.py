"""Incremental proactive passes: the store-only rules judged again only where the data moved.

A full proactive run (``operating.proactive_engine.run``) reads every record of the store:
up to an hour and several GB on the live store, so its findings trail the data. This pass
runs after each row-delta cycle (``ontology.delta``; ``deploy/systemd/plx-proactive-inc``,
every 5 minutes) and judges again only what the changes since the last pass can move, with
the same rules and the same code, so what it folds equals what a full run would fold:

1. **Changes** (``changes``): the records the delta layers own (``delta_owned`` of each
   layer the served pointer lists that this pass has not taken in yet; on Postgres only a
   layer Postgres has applied, ``ontology.load_state``), each fingerprinted in the store
   the engine reads (its type, audience, current versions' digests and markings, whether it
   is removed, and every current link at it): a record whose fingerprint is the one kept is
   unchanged (a compacted layer re-owning it changes nothing). Without a kept baseline for
   the served generation every owned record counts as changed (more work, never less).
2. **Subjects** (``plan``): per rule, the subjects whose declared inputs (``INPUTS``: the
   link types it reads, the hops from its subject, the identity links followed whole and
   the shared records it reads but never walks through) hold a changed record, by a walk
   back from the changed records and from the records they were linked to before (the
   generation's links and the kept ones: a link that went away moves both its ends), plus
   the subjects of the rule's open gaps that cite a changed record. Then **the scope**:
   every record those subjects read, by the same walk forward.
3. **Judge** (``proactive_engine.run(scope=)``): the rules run over the scope only (a
   whole-type read lists the scope's records, a link read the links at them) and keep the
   gaps about their own subjects; the fold writes those and resolves a subject's open gap
   it no longer finds. The run records, coverage counts and generation stay the full
   run's.

**Time**: every rule judges on the business day (ET, ``po_status.business_today``), so
within a day only data moves a claim; on the first pass of a new day the rules whose truth
moves with the date (due dates, expiry: ``DATED``) are judged again whole (an exact
re-evaluation of every dated claim, once a day), the others incrementally.

**Safety nets**: a scope over ``READ_CAP`` records, a rule without declared inputs or one
that reads more than its subject's links (``WHOLE``) is judged whole instead; a served
generation or engine code the inbox's full run has not read (``served_behind``) is left to
the full pass (``operating.vendor_watch --refresh new``), and a nightly full pass
(``plx-proactive``) re-reads everything. The engine's runs and this pass hold one lock per
inbox (``proactive_engine.run``): they never fold over each other.

**Watchlists**: the records that moved are also judged against the people's follows and
alert rules (``operating.watchlists``), whose alerts go to each owner's own inbox.

Read only on the store and the delta layers; writes only the inbox, its change file
``<inbox>.changes.sqlite`` (fingerprints, counts) and the watchlists file beside the store
(``<store>.watchlists.sqlite``: snapshots and alerts). Run::

    python3 -m operating.proactive_incremental --state-dir STATE [--as-of YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import resource
import sqlite3
import time
from typing import Any, Callable, Iterable, Mapping, Sequence

from ontology import store as ostore

from . import po_status
from . import proactive_engine as pe
from operating import declared_schema as _schemas
from operating.declared_schema import proactive_incremental as _PROACTIVE_INCREMENTAL

CHANGES_SUFFIX = ".changes.sqlite"
READ_CAP = 400_000   # a scope larger than this: its rules are judged whole instead
CHUNK = 500
# a pass finding the full engine running exits at once ("busy"): it kept what it had seen
# (the delta layers' fingerprints), so the next timer tick or publish judges everything that
# changed meanwhile. Waiting for a full pass held the unit for up to 15 minutes a tick
# (20 wall-hours of waiting in two days, CC-P-03); ``--wait-seconds`` still asks for it.
LOCK_WAIT_SECONDS = 0.0


@dataclass(frozen=True)
class Inputs:
    """What one rule's verdict on a subject reads: ``subjects`` (the types its gaps are
    about), ``links`` (every link type it reads, walked both ways up to ``hops`` from the
    subject), ``identity`` (link types whose clusters it reads whole: followed to the end,
    no hop counted) and ``hubs`` (links to a record many subjects share, a party or an
    item: read, never walked through, so only a change of that record walks back over
    them)."""
    subjects: tuple[str, ...]
    links: tuple[str, ...]
    hops: int
    identity: tuple[str, ...] = ()
    hubs: tuple[str, ...] = ()


# Per rule, its declared inputs (operating/proactive_engine.py and the modules it reads;
# a link type a rule reads that is missing here makes this pass wrong, so the equivalence
# test, operating/tests/test_proactive_incremental.py, compares a full run on every change
# it seeds). Filled from ``_declared`` (the engine's own link-type constants).
INPUTS: dict[str, Inputs] = {}
# Rules judged whole on every pass (they read what no link walk from a subject reaches).
WHOLE: frozenset[str] = frozenset()
# Rules whose truth or wording moves with the business day: judged whole on a new day.
DATED: frozenset[str] = frozenset()
# The rules an incremental pass judges: the store-only ones (the tool-call rules read
# through ``plx.process``/``plx.investigate`` and stay with the full and vendor passes,
# the customer-PO scan reads the Portal mirror, not the store).
STORE_ONLY = tuple(d for d in pe.DETECTORS
                   if d in pe.SET_BASED and d != "order_without_customer_po")
NOT_INCREMENTAL: frozenset[str] = frozenset()


def _declared() -> None:
    """Fill ``INPUTS``, ``WHOLE`` and ``DATED`` from the engine's constants (lazily: the
    schema's identity links are read once)."""
    global WHOLE, DATED, NOT_INCREMENTAL
    if INPUTS:
        return
    from . import declared_inputs
    INPUTS.update(declared_inputs.inputs())
    WHOLE = frozenset(declared_inputs.WHOLE)
    DATED = frozenset(declared_inputs.DATED)
    NOT_INCREMENTAL = frozenset(declared_inputs.NOT_INCREMENTAL)


# --------------------------------------------------------------------------- change file

KEEP_PASSES = 2000


def changes_path(inbox: Any) -> Path:
    inbox = Path(inbox)
    return inbox.with_name(inbox.name + CHANGES_SUFFIX)


def _open(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    _schemas.apply(conn, _PROACTIVE_INCREMENTAL)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return conn


def _meta(conn: sqlite3.Connection, key: str) -> Any:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return json.loads(row[0]) if row else None


def _set_meta(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, json.dumps(value)))


# --------------------------------------------------------------------------- changes

def _chunks(items: Sequence[Any], size: int = CHUNK) -> Iterable[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


def fingerprints(st: pe._Store, ids: Iterable[str]) -> dict[str, tuple[str, list[str]]]:
    """object id -> (fingerprint, the records it is linked to) as the store reads now: its
    type and audience, its current versions (handle, digest, markings), whether it is
    removed, and every current link at it (type, ends, tier, markings). A record the store
    does not hold has the fingerprint ``absent``."""
    ids = sorted(set(ids))
    out: dict[str, tuple[str, list[str]]] = {}
    for part in _chunks(ids):
        held = json.dumps(list(part))
        rows: dict[str, list[Any]] = {oid: [] for oid in part}
        found = set()
        for r in st.conn.execute(
                "SELECT o.object_id, o.object_type, o.audience_key, "
                + ostore.removed_at_sql("o.object_id") + " AS gone FROM objects o "
                "WHERE o.object_id IN (SELECT value FROM json_each(?))", (held,)):
            found.add(r[0])
            rows[r[0]].append(("o", r[1], r[2], bool(r[3])))
        for r in st.conn.execute(
                "SELECT object_id, l1_handle, digest, markings FROM object_versions "
                "WHERE superseded_at IS NULL AND object_id IN (SELECT value FROM json_each(?))",
                (held,)):
            rows[r[0]].append(("v", r[1], r[2], r[3]))
        neighbors: dict[str, set[str]] = {oid: set() for oid in part}
        for row in st.touching(part, every=True):
            _rowid, link_id, link_type, a, b, tier, marks, _gone = row
            for end, other in ((a, b), (b, a)):
                if end in rows:
                    rows[end].append(("l", link_id, link_type, a, b, tier, marks))
                    neighbors[end].add(other)
        for oid in part:
            if oid not in found:
                out[oid] = ("absent", sorted(neighbors[oid]))
                continue
            text = json.dumps(sorted(rows[oid], key=repr), sort_keys=True)
            out[oid] = (hashlib.sha256(text.encode()).hexdigest()[:32], sorted(neighbors[oid]))
    return out


def _pointer(store: Path) -> tuple[str | None, list[str]]:
    """(the base digest, the layer stores newest first) of the served delta pointer."""
    from . import delta_overlay

    try:
        raw = json.loads(delta_overlay.pointer_path(store).read_text())
    except (OSError, ValueError):
        return None, []
    if not isinstance(raw, dict):
        return None, []
    layers = [str(layer.get("store")) for layer in raw.get("layers") or ()
              if isinstance(layer, dict) and layer.get("store")]
    if not layers and raw.get("store"):
        layers = [str(raw["store"])]
    return raw.get("base_digest"), layers


def _applied(store: Path) -> set[str] | None:
    """The layer file names Postgres has applied when the engine reads Postgres
    (``operating.pg.ontology_delta``); None: the engine reads the store file itself."""
    from operating.pg import ontology_reader

    if not ontology_reader.serves(store):
        return None
    import psycopg

    with psycopg.connect(ontology_reader.dsn(), connect_timeout=10) as conn:
        return {row[0] for row in conn.execute(
            "SELECT substr(name, 7) FROM ontology.load_state WHERE name LIKE 'delta:%'")}


def layer_owned(store: Path, digest: str | None, ch: sqlite3.Connection,
                applied: set[str] | None) -> tuple[set[str], list[str], bool]:
    """(the records owned by the served layers not taken in yet, those layers, whether a
    layer is still waiting for Postgres): a layer of another generation is never read.
    ``applied``: the layers the store read holds (Postgres's applied ones; None: every
    layer, a store built with them)."""
    base, layers = _pointer(store)
    if base is None or base != digest:
        return set(), [], False
    seen = {row[0] for row in ch.execute("SELECT layer FROM layers WHERE base_digest = ?",
                                         (digest,))}
    owned: set[str] = set()
    taken, waiting = [], False
    for layer in layers:
        if layer in seen:
            continue
        if applied is not None and Path(layer).name not in applied:
            waiting = True   # not in Postgres yet: the next pass takes it in
            continue
        try:
            with closing(sqlite3.connect(f"file:{layer}?mode=ro", uri=True)) as conn:
                owned.update(row[0] for row in conn.execute("SELECT object_id FROM delta_owned"))
        except sqlite3.Error:
            waiting = True   # pruned or unreadable now: a later layer holds its records
            continue
        taken.append(layer)
    return owned, taken, waiting


def _base_neighbors(base: Path | None, ids: Sequence[str]) -> dict[str, set[str]]:
    """The records each of ``ids`` is linked to in the generation file ``base`` (what the
    full run's data held before any layer), read by the link end indexes."""
    out: dict[str, set[str]] = {}
    if base is None or not Path(base).is_file():
        return out
    with closing(sqlite3.connect(f"file:{base}?mode=ro", uri=True)) as conn:
        for part in _chunks(sorted(ids)):
            held = json.dumps(list(part))
            for end, other in (("from_object", "to_object"), ("to_object", "from_object")):
                for a, b in conn.execute(
                        f"SELECT {end}, {other} FROM link_rows WHERE superseded_at IS NULL "
                        f"AND {end} IN (SELECT value FROM json_each(?))", (held,)):
                    out.setdefault(a, set()).add(b)
    return out


# --------------------------------------------------------------------------- the scope

def _walk(st: pe._Store, start: Iterable[str], spec: Inputs, *, back: bool,
          links: dict[str, list[tuple]]) -> set[str]:
    """Every record within ``spec.hops`` link hops of ``start`` over the rule's links,
    identity clusters taken whole at each step. Forward (``back`` False: what a subject
    reads), a shared record reached by a hub link is read, never walked through; back
    (what a changed record moves), a hub link is walked only from a changed record itself."""
    kinds = set(spec.links) | set(spec.identity) | set(spec.hubs)
    hubs, identity = set(spec.hubs), set(spec.identity)

    def at(ids: Iterable[str]) -> list[tuple]:
        need = [i for i in ids if i not in links]
        if need:
            for oid in need:
                links[oid] = []
            for row in st.touching(need):
                links.setdefault(row[3], []).append(row) if row[3] in links else None
                if row[4] != row[3]:
                    links.setdefault(row[4], []).append(row) if row[4] in links else None
        return [row for i in ids for row in links[i]]

    def cluster(ids: set[str]) -> set[str]:
        grown, edge = set(ids), set(ids)
        while edge and identity:
            nxt = set()
            for row in at(edge):
                if row[2] in identity:
                    nxt.update((row[3], row[4]))
            edge = nxt - grown
            grown |= edge
        return grown

    reached = cluster(set(start))
    frontier = set(reached)
    seeds = set(start)
    terminal: set[str] = set()
    for _hop in range(spec.hops):
        nxt: set[str] = set()
        for node in frontier - terminal:
            for row in links.get(node) or at([node]):
                if row[2] not in kinds:
                    continue
                other = row[4] if row[3] == node else row[3]
                if row[2] in hubs:
                    if back and node not in seeds:
                        continue
                    if not back:
                        terminal.add(other)
                nxt.add(other)
        nxt = cluster(nxt - reached) if nxt - reached else set()
        frontier = nxt
        reached |= nxt
        if not frontier:
            break
    return reached


def _types(st: pe._Store, ids: Iterable[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in _chunks(sorted(set(ids))):
        for oid, kind in st.conn.execute(
                "SELECT object_id, object_type FROM objects WHERE object_id IN "
                "(SELECT value FROM json_each(?))", (json.dumps(list(part)),)):
            out[oid] = kind
    return out


def plan(st: pe._Store, changed: set[str], before: Mapping[str, set[str]],
         inbox: Path, rules: Sequence[str]) -> tuple[dict[str, frozenset[str]], set[str]]:
    """({rule: the subjects it judges again}, the records they read) for the records that
    changed (``changed``) and the records each was linked to before (``before``)."""
    _declared()
    seeds = set(changed)
    for oid in changed:
        seeds |= before.get(oid, set())
    cited: dict[str, set[str]] = {}
    if inbox.is_file():
        with closing(sqlite3.connect(f"file:{inbox}?mode=ro", uri=True)) as conn:
            for part in _chunks(sorted(seeds)):
                for detector, subject in conn.execute(
                        "SELECT DISTINCT g.detector, g.object_id FROM gaps g JOIN gap_cites c "
                        "ON c.gap_id = g.gap_id WHERE g.state = 'open' AND c.object_id IN "
                        "(SELECT value FROM json_each(?))", (json.dumps(list(part)),)):
                    cited.setdefault(detector, set()).add(subject)
    links: dict[str, list[tuple]] = {}
    moved: dict[str, set[str]] = {}
    for rule in rules:
        moved[rule] = _walk(st, seeds, INPUTS[rule], back=True, links=links) \
            | cited.get(rule, set())
    kinds = _types(st, set().union(*moved.values()) if moved else ())
    subjects: dict[str, frozenset[str]] = {}
    read: set[str] = set()
    for rule in rules:
        spec = INPUTS[rule]
        mine = frozenset(oid for oid in moved[rule]
                         if kinds.get(oid) in spec.subjects or "*" in spec.subjects
                         or oid in cited.get(rule, ()))
        subjects[rule] = mine
        if mine:
            read |= _walk(st, mine, spec, back=False, links=links)
    return subjects, read


def _audience_keys(st: pe._Store, ch: sqlite3.Connection, digest: str | None,
                   changed: Iterable[str]) -> frozenset[str]:
    """The store's audience keys: the generation's (read whole once per generation, kept)
    and those of the records changed since."""
    held = _meta(ch, "audience_keys")
    if not isinstance(held, dict) or held.get("digest") != digest:
        held = {"digest": digest, "keys": sorted(pe._Store.audience_keys(st))}
    keys = set(held["keys"])
    for part in _chunks(sorted(set(changed))):
        keys.update(r[0] for r in st.conn.execute(
            "SELECT DISTINCT audience_key FROM objects WHERE object_id IN "
            "(SELECT value FROM json_each(?))", (json.dumps(list(part)),)))
    held["keys"] = sorted(keys)
    _set_meta(ch, "audience_keys", held)
    return frozenset(keys)


# --------------------------------------------------------------------------- one pass

def _rss_mb() -> float:
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)


def run_pass(store_path: Any, *, as_of: date | None = None,
             clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
             changed: Iterable[str] | None = None, base: Any = None,
             people: Any = None, wait: bool | float = LOCK_WAIT_SECONDS,
             read_cap: int = READ_CAP, holds_layers: bool | None = None) -> dict[str, Any]:
    """One incremental pass over the store served at ``store_path`` into its inbox; counts
    and enums only. ``changed``: the records that changed since the last pass (a test or a
    replay; default the delta layers', ``layer_owned``, fingerprinted); ``base``: the
    generation file whose links the changed records had before (default ``store_path``).
    ``wait``: how long to wait for a running engine pass before returning ``busy`` (True:
    until it ends; False: not at all).
    ``holds_layers``: whether the store read holds the delta layers (None: when the engine
    reads Postgres, its applied ones); a store that does not (the SQLite generation) takes
    no layer in, so none is marked seen before a store holding it is read."""
    _declared()
    store = Path(store_path)
    inbox = pe.inbox_path(store)
    started = time.monotonic()
    moment = clock()
    day = po_status.business_today(moment) if as_of is None else as_of
    out: dict[str, Any] = {"as_of": day.isoformat()}
    if not inbox.is_file():
        return {**out, "state": "no_inbox"}   # no full run yet: the full pass makes it
    with pe.pass_lock(inbox, wait=wait) as held:
        if not held:
            return {**out, "state": "busy"}
        # a rule whose findings are of another generation or older code is judged whole
        # (read under the lock: a full pass waited for has just brought it current)
        behind = pe.rules_behind(store, inbox) & set(STORE_ONLY)
        with closing(_open(changes_path(inbox))) as ch:
            st = pe._Store(store)
            try:
                digest = st.meta("content_digest")
                if _meta(ch, "digest") != digest:
                    # another generation: nothing kept is of it
                    ch.execute("DELETE FROM prints")
                    ch.execute("DELETE FROM layers")
                    _set_meta(ch, "digest", digest)
                taken: list[str] = []
                waiting = False
                applied = _applied(store) if holds_layers is None else None
                reader = "postgres" if applied is not None else (
                    "store_with_layers" if holds_layers else "generation_only")
                if _meta(ch, "reader") != reader:
                    # another store read: nothing kept was fingerprinted in it
                    ch.execute("DELETE FROM prints")
                    ch.execute("DELETE FROM layers")
                    _set_meta(ch, "reader", reader)
                out["reader"] = reader
                if changed is None and reader == "generation_only":
                    # the layers' changes are not in what the engine reads: none is taken
                    # in (time and rules behind still are judged, below)
                    owned, taken, waiting = set(), [], bool(_pointer(store)[1])
                    prints, moved = {}, set()
                elif changed is None:
                    owned, taken, waiting = layer_owned(store, digest, ch, applied)
                    prints = fingerprints(st, owned)
                    kept = {}
                    for part in _chunks(sorted(prints)):
                        kept.update({r[0]: r[1] for r in ch.execute(
                            "SELECT object_id, print FROM prints WHERE object_id IN "
                            "(SELECT value FROM json_each(?))", (json.dumps(list(part)),))})
                    moved = {oid for oid, (fp, _n) in prints.items() if kept.get(oid) != fp}
                else:
                    moved = set(changed)
                    prints = fingerprints(st, moved)
                out["candidates"] = len(prints)
                out["changed"] = len(moved)
                before: dict[str, set[str]] = _base_neighbors(
                    Path(base) if base is not None else store, sorted(moved))
                for part in _chunks(sorted(moved)):
                    for r in ch.execute("SELECT object_id, neighbors FROM prints WHERE "
                                        "object_id IN (SELECT value FROM json_each(?))",
                                        (json.dumps(list(part)),)):
                        before.setdefault(r[0], set()).update(json.loads(r[1]))
                last_day = _meta(ch, "day") or _last_run_day(inbox)
                new_day = last_day is None or day.isoformat() > last_day
                whole = sorted(set(WHOLE) | (set(DATED) if new_day else set()) | behind)
                rules = [r for r in STORE_ONLY if r not in whole and r in INPUTS
                         and r not in NOT_INCREMENTAL]
                subjects, read = (plan(st, moved, before, inbox, rules) if moved
                                  else ({}, set()))
                if len(read) > read_cap:
                    whole = sorted(set(whole) | set(rules))
                    subjects, read = {}, set()
                keys = _audience_keys(st, ch, digest, moved) if subjects else None
                out["watchlists"] = _watch(st, store, moved, clock)
            finally:
                st.close()
            out["planned_seconds"] = round(time.monotonic() - started, 2)
            ran: dict[str, Any] = {}
            busy = {rule: s for rule, s in subjects.items() if s}
            if busy:
                found = pe.run(store, inbox, day, clock=clock, people=people,
                               scope=pe.Scope(busy, frozenset(read), keys), locked=True)
                ran["scoped"] = {"subjects": {r: len(s) for r, s in sorted(busy.items())},
                                 "read": len(read), "gaps_found": found.get("gaps_found"),
                                 "seconds": round(time.monotonic() - started, 2)}
                # a rule whose population moved is judged whole (below)
                whole = sorted(set(whole) | set(found.get("population_moved") or ()))
                if found.get("populations_moved"):
                    ran["populations_moved"] = found["populations_moved"]
            whole = [r for r in STORE_ONLY if r in whole]
            if whole:
                found = pe.run(store, inbox, day, clock=clock, only=whole, people=people,
                               threads=None, locked=True)
                ran["whole"] = {"rules": whole, "gaps_found": found.get("gaps_found"),
                                "detectors": found.get("detectors")}
            # what was judged is what is kept: the next pass compares with it
            ch.execute("BEGIN IMMEDIATE")
            ch.executemany("INSERT OR REPLACE INTO prints VALUES (?, ?, ?)",
                           [(oid, fp, json.dumps(n)) for oid, (fp, n) in prints.items()])
            ch.executemany("INSERT OR IGNORE INTO layers VALUES (?, ?, ?)",
                           [(layer, digest, moment.isoformat()) for layer in taken])
            if new_day and set(DATED) & set(STORE_ONLY) <= set(whole):
                _set_meta(ch, "day", day.isoformat())
            out.update(state="ran", waiting_for_postgres=waiting, **ran,
                       seconds=round(time.monotonic() - started, 2), max_rss_mb=_rss_mb())
            ch.execute("INSERT INTO passes (at, summary) VALUES (?, ?)",
                       (moment.isoformat(), json.dumps(out, sort_keys=True)))
            ch.execute("DELETE FROM passes WHERE pass_id NOT IN (SELECT pass_id FROM passes "
                       "ORDER BY pass_id DESC LIMIT ?)", (KEEP_PASSES,))
            ch.execute("COMMIT")
    return out


def _watch(st: pe._Store, store: Path, moved: set[str],
           clock: Callable[[], datetime]) -> dict[str, Any]:
    """The people's follows and alert rules judged over what moved
    (``operating.watchlists``): counts only; a failure is reported and leaves the pass's
    own work untouched (no alert is written, the snapshots stay as they were)."""
    from . import watchlists

    try:
        return watchlists.evaluate(st, watchlists.watch_path(store), moved, clock=clock)
    except Exception as error:  # noqa: BLE001 - the watchlists never stop the pass
        return {"state": "failed", "error": type(error).__name__}


def _last_run_day(inbox: Path) -> str | None:
    """The day the inbox's last full run judged on."""
    with closing(sqlite3.connect(f"file:{inbox}?mode=ro", uri=True)) as conn:
        try:
            row = conn.execute("SELECT as_of FROM runs ORDER BY run_id DESC LIMIT 1").fetchone()
        except sqlite3.Error:
            return None
    return row[0] if row else None


def main(argv: Sequence[str] | None = None) -> int:
    from .host import projection_path

    parser = argparse.ArgumentParser(prog="python3 -m operating.proactive_incremental",
                                     description=__doc__.split("\n\n")[0])
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--as-of", type=date.fromisoformat, default=None)
    parser.add_argument("--wait", action="store_true",
                        help="wait for a running engine pass however long it takes")
    parser.add_argument("--wait-seconds", type=float, default=LOCK_WAIT_SECONDS,
                        help="wait this long for a running engine pass before skipping "
                             f"(default {LOCK_WAIT_SECONDS:.0f})")
    args = parser.parse_args(argv)
    if args.wait_seconds < 0:
        parser.error("--wait-seconds must not be negative")
    print(json.dumps(run_pass(projection_path(args.state_dir), as_of=args.as_of,
                              wait=True if args.wait else args.wait_seconds),
                     indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
