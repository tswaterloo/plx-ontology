"""The workspace backend's screen serving: suggestions, kept copies, access checks, refresh."""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future, TimeoutError as FutureTimeout
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

from . import errors, models as m
from .auth import Reader
from .serving import (
    LAST_GOOD_SECONDS, READ_TIMEOUT_SECONDS, SNAPSHOT_RECENT_SECONDS, SNAPSHOT_REFRESH_KEPT
)
from operating.po_pilot import bc_pilot_object_sql
from .suggest import (
    ITEM_CHECK_SECONDS, party_codes, projection_suggest, SAME_RECORD_SECONDS, same_records,
    SameRecordMemo, SUGGEST_FUZZY, suggest_phrases, SUGGEST_ROWS, SUGGEST_TYPE_RANK, suggestions
)
from .page_parts import shown_objects

from .backend_common import UNSERVABLE_STATUSES
from .backend_pages import _kept_good_model, _portable_model, _ReadByOtherCode



def own_issued_kept(rows: list[tuple]) -> list[tuple]:
    """Suggestion ``rows`` (the object id at 1) without the PLX-issued sales documents the
    served store holds as supplier documents (``operating.pg.own_issued``), hidden from
    every reader as every visible-object read hides them."""
    from operating.pg.own_issued import hidden_ids
    hidden = hidden_ids()
    return [r for r in rows if r[1] not in hidden] if hidden else rows


class ServingMixin:
    """Serving screens: suggestions, kept copies, access checks and refresh."""

    # ---- suggest ----------------------------------------------------------------------

    def _reader_policy(self, reader: Reader) -> tuple[Any, Any]:
        """(gated actor, ``ReadPolicy``) of the reader now, from its own workspace on the
        read worker. Never cached here: the ``ScopeCache`` keys the scope on the
        policy's fingerprint and the exclusion, so a revoked grant changes the key at once."""
        def job(view: Any) -> tuple[Any, Any]:
            with view._request():
                return view.access.reader_policy()
        # a keystroke's search waits on this: on the access worker (the readers' policies
        # are decided there first), else ahead of the queued page reads, never behind them
        # (live 11-18: Records search answered "busy" behind a record page's section reads)
        worker = getattr(self, "access_worker", None)
        if worker is None or worker.stopped.is_set():
            worker = self.reads
        return worker.run(reader, job, timeout=10, priority=-1)

    def _scope(self, conn: Any, reader: Reader) -> Any:
        from operating.pg.permissions import reader_scope
        gated, policy = self._reader_policy(reader)
        return reader_scope(conn, policy, gated)

    def _pg_suggest(self, reader: Reader, q: str, limit: int, *,
                    members: Optional[dict[str, list[str]]] = None
                    ) -> Optional[list[m.Suggestion]]:
        """``search.entities`` through the reader's scope (before ranking); None when Postgres holds no entities yet.
        ``q``: the words typed up to the caret; each trailing phrase of them is looked up,
        longest first ("three ships", then "ships"), so a name of several words is found
        whole. A match is an exact name, a record code, a name starting with the phrase, a
        word of the name starting with it, or (four letters or more) a near spelling of a
        word of it (``pg_trgm`` word similarity at least ``SUGGEST_FUZZY``). Copies of one
        record collapse (``suggestions``)."""
        pool = self.pg()
        if pool is None:
            return None
        phrases = suggest_phrases(q)
        if not phrases:
            return []
        rows: list[tuple] = []
        try:
            with pool.connection() as conn:
                if conn.execute("SELECT NOT EXISTS (SELECT 1 FROM search.entities)"
                                ).fetchone()[0]:
                    return None
                scope = self._scope(conn, reader)
                if scope.empty:
                    return []
                conn.execute("SET pg_trgm.word_similarity_threshold = %s"
                             % float(SUGGEST_FUZZY))
                type_order = "CASE e.type " + " ".join(
                    f"WHEN '{kind}' THEN {rank}" for kind, rank in SUGGEST_TYPE_RANK.items()
                ) + " ELSE 9 END"
                for words, needle in phrases:
                    plain_needle = needle.replace("%", "").replace("_", "")
                    # a near spelling of one or two words, never of a whole clause
                    fuzzy = len(needle) >= 4 and words <= 2
                    found = conn.execute(
                        "SELECT e.object_id, e.type, e.name, e.alias, e.audience_key, "
                        "e.normalized = %(q)s AS exact, "
                        "e.alias @> ARRAY[%(code)s]::text[] OR e.alias @> ARRAY[%(raw)s]::text[]"
                        " AS code, e.normalized LIKE %(p)s AS prefix, "
                        "e.normalized LIKE %(w)s AS word, "
                        "word_similarity(%(q)s, e.normalized) AS s "
                        "FROM search.entities e WHERE " + scope.clause("e") +
                        " AND e.object_id IS NOT NULL AND (e.normalized = %(q)s OR "
                        "e.normalized LIKE %(p)s OR e.normalized LIKE %(w)s OR "
                        "e.alias @> ARRAY[%(code)s]::text[] OR e.alias @> ARRAY[%(raw)s]::text[]"
                        + (" OR %(q)s <%% e.normalized" if fuzzy else "") +
                        ") ORDER BY exact DESC, code DESC, prefix DESC, word DESC, "
                        + type_order + ", " + bc_pilot_object_sql("e.object_id")
                        + ", s DESC, length(e.name), e.name LIMIT %(n)s",
                        scope.params(q=needle, p=plain_needle + "%", w="% " + plain_needle + "%",
                                     code=needle.upper(), raw=needle,
                                     n=SUGGEST_ROWS)).fetchall()
                    rows += [(words, *row) for row in found]
                    strong = {r[1] for r in rows if any(r[6:10])}
                    if len(strong) >= SUGGEST_ROWS:
                        break   # enough whole-phrase matches: shorter phrases rank below
                # an order's supplier or customer by name, its code beside it: the party
                # entities that carry the orders' party codes, within the same scope
                codes = sorted({code for r in rows for code in party_codes(r[4])})[:40]
                from operating.query_understanding import item_families
                gated_families = sorted(item_families())
                parties: dict[str, str] = {}
                if codes:
                    for name, alias in conn.execute(
                            "SELECT e.name, e.alias FROM search.entities e WHERE "
                            + scope.clause("e") + " AND e.type IN ('supplier', 'customer') "
                            "AND split_part(e.audience_key, ':', 1) <> ALL(%(gated)s::text[]) "
                            "AND e.alias && %(codes)s::text[] ORDER BY e.name",
                            scope.params(codes=codes, gated=gated_families)).fetchall():
                        for code in set(alias or ()) & set(codes):
                            parties.setdefault(code, str(name))
        except (TimeoutError, LookupError):
            raise
        except Exception:  # noqa: BLE001 - Postgres down: the projection answers
            return None
        rows = self._item_checked_suggestions(reader, own_issued_kept(rows))
        return suggestions(rows, limit, members=members, parties=parties,
                           same_record=lambda ids: self._same_record_pairs(reader, ids))

    def _item_checked_suggestions(self, reader: Reader, rows: list[tuple]) -> list[tuple]:
        """``rows`` (``_pg_suggest``'s, the audience key at 5) without it, each row of an
        item-gated source (a mail or Teams participant, a file or chat name) kept only
        when this reader's read opens its record (``visible_objects``: the item permission
        a unit grant does not decide); an unreadable decision keeps none of them."""
        from operating.call_limits import SQL_GRACE
        from operating.query_understanding import item_gated
        gated = sorted({r[1] for r in rows if item_gated(r[5])})
        opens: frozenset[str] = frozenset()
        if gated:
            def job(view: Any) -> frozenset[str]:
                with view._request():
                    seen = view.access.visible_objects(gated)
                return frozenset(seen) if isinstance(seen, (set, frozenset)) else frozenset()
            # a keystroke waits for the item check as long as for the same-record walk
            # (ITEM_CHECK_SECONDS), never a full read: undecided, the gated rows are hidden
            future = self.reads.submit(
                reader, job, priority=-1,
                deadline=time.monotonic() + ITEM_CHECK_SECONDS + SQL_GRACE)
            try:
                got = future.result(timeout=ITEM_CHECK_SECONDS)
                opens = got if isinstance(got, frozenset) else frozenset()
            except Exception:  # noqa: BLE001 - undecided or failed: no item-gated suggestion
                future.cancel()
                opens = frozenset()
        return [(*r[:5], *r[6:]) for r in rows if not item_gated(r[5]) or r[1] in opens]

    def _same_record_pairs(self, reader: Reader, ids: Sequence[str]) -> set[frozenset[str]]:
        """The suggestion copies this reader reads as one record: the kept walks at once;
        records not walked yet are walked on a read worker, waited for at most
        ``SAME_RECORD_SECONDS`` and kept as each is read, so the next keystroke has them
        (until then their copies stay apart)."""
        memo = self.__dict__.setdefault("_same_record_memo", SameRecordMemo())
        if not memo.missing(reader.key, ids):
            return memo.pairs(reader.key, ids)
        future = self.reads.submit(reader, lambda view: same_records(
            view, ids, memo=memo, reader=reader.key), priority=-1)
        try:
            return future.result(timeout=SAME_RECORD_SECONDS)
        except FutureTimeout:
            return memo.pairs(reader.key, ids)

    SUGGEST_KEPT_SECONDS = 20.0
    SUGGEST_KEPT_MOST = 256

    def suggest(self, reader: Reader, q: str, limit: int, *, person: bool = False
                ) -> m.SuggestResponse:
        """``_suggest`` kept briefly per (reader, query, limit) for the person's own search
        box: a repeat (the same word again, a back and forth, a Refresh) is answered from the
        copy while the reader's access state (``_access_now``: policy fingerprint,
        unrestricted, people exclusion, document markings) and the served inputs are exactly
        those it was read under, confirmed before and after that read. Only a business-table
        answer is kept (its rows are decided by the policy alone); a record-search answer
        holds item-level (mail, Teams, file) rows whose permissions are in no state, and a
        viewing-as session reads and keeps nothing. Never kept when the access state
        could not be read."""
        session = getattr(reader, "session", None)
        if not person or reader is None or getattr(session, "view_as", False) \
                or not callable(getattr(self, "_access_now", None)):
            return self._suggest(reader, q, limit, person=person)
        started = time.perf_counter()
        key = (reader.key, getattr(reader, "mode", None), q, limit)
        before = self._suggest_access(reader)
        inputs = self.inputs_stamp()
        now = time.monotonic()
        with self._lock:
            kept = self.__dict__.setdefault("_suggest_kept", {})
            hit = kept.get(key)
        if hit is not None and before is not None and inputs is not None \
                and now - hit[0] < self.SUGGEST_KEPT_SECONDS and hit[1] == before \
                and hit[2] == inputs:
            return hit[3].model_copy(update={
                "took_ms": int((time.perf_counter() - started) * 1000)})
        out = self._suggest(reader, q, limit, person=True)
        if before is not None and inputs is not None and out.source == "business" \
                and self._suggest_access(reader) == before and self.inputs_stamp() == inputs:
            with self._lock:
                kept = self.__dict__.setdefault("_suggest_kept", {})
                kept[key] = (now, before, inputs, out)
                if len(kept) > self.SUGGEST_KEPT_MOST:
                    for k in sorted(kept, key=lambda k: kept[k][0])[:len(kept) // 4]:
                        kept.pop(k, None)
        return out

    def _suggest_access(self, reader: Reader) -> Optional[tuple]:
        """``_access_now`` with the PLX-issued documents' digest (a search answer's rows are
        not named one by one: a new PLX-issued document reads it again); None: unreadable."""
        from .backend_pages import own_issued_stamp
        state, own = self._access_now(reader), own_issued_stamp()
        return None if state is None or own is None else (state, own)

    def _suggest(self, reader: Reader, q: str, limit: int, *, person: bool = False
                 ) -> m.SuggestResponse:
        """``person``: the person's own search box (``/api/suggest``), whose answer only
        that person reads. Only then the business-table search (``page_sql.search``, the
        person-only views: Freightcom rows a person may see). Every other caller (an
        instruction or a form a model works on) reads the record search below, so nothing
        a person-only view holds reaches a model through a lookup."""
        started = time.perf_counter()
        from . import page_sql
        if person and page_sql.on():
            # the one search box over the business tables, through the reader's door; an
            # unreadable door falls back to the record search below (never "no match")
            try:
                found = page_sql.search(self, reader, q, limit)
            except page_sql.PageDoorUnavailable:
                found = None
            if found is not None:
                return m.SuggestResponse(query=q, suggestions=found, source="business",
                                         took_ms=int((time.perf_counter() - started) * 1000))
        found = self._pg_suggest(reader, q, limit)
        source = "search_entities"
        if found is None:
            source = "projection"
            found = self.reads.run(reader, lambda view: projection_suggest(view, q, limit),
                                   timeout=READ_TIMEOUT_SECONDS)
        return m.SuggestResponse(query=q, suggestions=found, source=source,
                                 took_ms=int((time.perf_counter() - started) * 1000))

    def biz_page(self, reader: Reader, kind: str, handle: str = "", chain: str = "",
                 record: str = "") -> m.BizPage:
        """An account page (customer, chain, supplier) or an order page (PO, sales order)
        from the business tables through the reader's door (switch ``PLX_PAGES_DOOR``).
        ``record``: a record id instead of the handle (the page its row opens)."""
        from . import page_sql
        if not page_sql.on(kind):
            raise errors.UserFacing("pages_off", "These record pages aren't switched on on "
                                    "this server yet.", 404)

        def read() -> m.BizPage:
            try:
                at = page_sql.resolve(self, reader, kind, record) \
                    if record and not handle and kind != "chain" else handle
                if kind in ("purchase_order", "sales_order"):
                    return page_sql.order(self, reader, kind, at)
                if kind == "product":
                    return page_sql.item(self, reader, at)
                if kind == "batch":
                    return page_sql.batch(self, reader, at)
                return page_sql.account(self, reader, kind, handle=at, chain=chain)
            except page_sql.NotFound:
                raise errors.UserFacing("not_found", "No such record that you can open.",
                                        404) from None
            except page_sql.PageDoorUnavailable as exc:
                why = ("took too long" if exc.code == "timeout" else
                       "is busy" if exc.code == "busy" else
                       "isn't built yet" if exc.code == "not_ready" else "couldn't be read")
                raise errors.UserFacing("page_unavailable", f"The record's figures {why}; "
                                        "try again in a minute.", 503) from None

        # kept per reader as every screen is: the door's read took 1.5-1.7 s on each open of
        # the order Home's detail pane shows (11-19 cold round), after a restart too
        return self._serving(reader, ("biz_page", kind, handle, chain, record), read,
                             self._screen_inputs())

    def records_list(self, reader: Reader, view: str, status: str = "", hold: str = "",
                     q: str = "", offset: int = 0, limit: int = 25) -> m.RecordList:
        """A Records list (``page_sql.record_list``) through the reader's door, kept per
        reader and filter as every screen is."""
        from . import page_sql
        if not page_sql.on("batch"):
            raise errors.UserFacing("pages_off", "These lists aren't switched on on this "
                                    "server yet.", 404)

        def read() -> m.RecordList:
            try:
                return page_sql.record_list(self, reader, view, status=status, hold=hold, q=q,
                                            offset=offset, limit=limit)
            except page_sql.NotFound:
                raise errors.UserFacing("not_found", "No such list.", 404) from None
            except page_sql.PageDoorUnavailable as exc:
                why = ("took too long" if exc.code == "timeout" else
                       "is busy" if exc.code == "busy" else "couldn't be read")
                raise errors.UserFacing("page_unavailable", f"The list {why}; try again in a "
                                        "minute.", 503) from None
        return self._serving(reader, ("records_list", view, status, hold, q, offset, limit),
                             read, self._screen_inputs())

    def test_named(self) -> frozenset[str]:
        """The deployment's named test records (``operator_api.test_records``)."""
        from .test_records import listed
        return listed(self.data_dir)

    def _pg_load_stamp(self) -> Any:
        """Over Postgres: the load state rows' count and newest write (None without one;
        an unreadable one raises)."""
        pool = self.pg() if self.pg_dsn else None
        if pool is None:
            return None
        with pool.connection() as conn:
            row = conn.execute("SELECT count(*), max(updated_at) FROM ontology.load_state"
                               ).fetchone()
        return (int(row[0]), str(row[1]))

    def _inputs_of(self, reader: Reader, inputs_of: Optional[Callable[[], Any]] = None,
                   screen: Any = None) -> Any:
        """``inputs_stamp`` with the count of the reader's own writes (``reader_wrote``:
        every one, and those that name ``screen``) and the screen's own inputs
        (``inputs_of``); None without a stamp or when the screen's own cannot be read
        (nothing is current then)."""
        stamp = self.inputs_stamp()
        if stamp is None:
            return None
        own = None
        if inputs_of is not None:
            try:
                own = inputs_of()
            except Exception:  # noqa: BLE001 - unreadable inputs: nothing is current
                return None
            if own is None:
                return None
        with self._lock:
            writes = self.__dict__.setdefault("_reader_writes", {})
            return (stamp, (writes.get(reader.key, 0), writes.get((reader.key, screen), 0)),
                    own)

    def reader_wrote(self, reader: Any, screens: Optional[Iterable[str]] = None) -> None:
        """The reader changed something their own screens show (any request that is not a
        read, before and after it runs; an Ask answered): none of their kept pages is
        current any more, and the next visit reads (a page read before keeps nothing
        current either). ``screens``: only those screens (the first part of a screen's
        name: a briefing marked seen changes the briefing and Home, not My work)."""
        key = getattr(reader, "key", None)
        if key is None:
            return
        with self._lock:
            writes = self.__dict__.setdefault("_reader_writes", {})
            for one in ([key] if screens is None else [(key, s) for s in screens]):
                writes[one] = writes.get(one, 0) + 1

    def reread_after_write(self, reader: Any, screens: Optional[Iterable[str]]) -> int:
        """After a write that changed only ``screens`` of the reader's (Home's seen mark:
        the briefing and Home), read those of their screens served lately again now, off
        the request, so their next visit is served a kept page rather than paying the read
        (measured on live data: briefing 1.25 s, Home 0.6 s on the visit after each mark).
        A write of every screen (``screens`` None) reads nothing ahead. How many started."""
        key = getattr(reader, "key", None)
        if key is None or screens is None:
            return 0
        names = frozenset(screens)
        with self._lock:
            again = [(k, v) for k, v in self._snap_reads().items()
                     if k[0] == key and k[2] and k[2][0] in names]
        started = 0
        for k, (remembered, read, *_) in again:
            if self._may_read_ahead(remembered):
                self._refresh_later(remembered, k, read)
                started += 1
        return started

    def _snap_inputs(self) -> dict:
        return self.__dict__.setdefault("_good_inputs", {})

    def _snap_reads(self) -> dict:
        return self.__dict__.setdefault("_good_reads", {})

    def _watch_inputs(self) -> None:
        """Start, once, the thread that reads the recent screens again when the inputs
        change (``_refresh_changed``)."""
        with self._lock:
            if getattr(self, "_inputs_watch", None) is not None:
                return
            self._inputs_watch = threading.Thread(target=self._refresh_on_change,
                                                  name="snapshot-inputs", daemon=True)
        self._inputs_watch.start()

    def _refresh_changed(self) -> int:
        """Read again, one at a time, off any request and only while no request is being
        read (``wait_idle``), the screens served within
        ``SNAPSHOT_RECENT_SECONDS`` (newest first, at most ``SNAPSHOT_REFRESH_KEPT``) whose
        kept page was read under other inputs; each kept as a fresh read is (the access
        state the same before and after). The count read."""
        stamp = self.inputs_stamp()
        cut = time.monotonic() - SNAPSHOT_RECENT_SECONDS
        with self._lock:
            under = self._snap_inputs()
            recent = sorted(((k, v) for k, v in self._snap_reads().items()
                             if v[2] >= cut and (under.get(k) or (None,))[0] != stamp),
                            key=lambda kv: -kv[1][2])[:SNAPSHOT_REFRESH_KEPT]
        done = 0
        for key, (reader, read, *_) in recent:
            if not self._may_read_ahead(reader):
                continue
            # a live request first: each screen waits until the read workers are idle
            # (a refresh read never queues ahead of, or beside, a reader's own read)
            if getattr(self, "yield_to_live", False):
                self.wait_idle()
            if self._refresh_now(reader, key, read):
                done += 1
        return done

    def _may_read_ahead(self, reader: Reader) -> bool:
        """Whether a screen may be read for ``reader`` with no request of theirs: a real
        session (not a read-ahead one) still unexpired whose ticket is not revoked (the
        replay guard, as ``warm_known``; without one, every ticket counts as revoked)."""
        session = getattr(reader, "session", None)
        ref, exp = getattr(session, "session_ref", None), getattr(session, "expires_at", None)
        if not isinstance(ref, str) or not isinstance(exp, int) or exp <= time.time() \
                or getattr(session, "warm_ahead", False) or getattr(session, "view_as", False):
            return False
        try:
            return not self.revoked(ref)
        except Exception:  # noqa: BLE001 - undecidable: not read ahead
            return False

    def _refused(self, reader: Reader, key: tuple, held: Optional[tuple], before: Future,
                 read: Callable[[], Any], error: Exception, refresh: bool = True) -> Any:
        """A read that timed out or was refused busy: the reader's last good page (read
        again in the background), else the refusal. ``refresh`` False: a request that
        joined another's read (``_screen_read``), whose own refusal starts the one read
        again; the joiners start none beside it."""
        if isinstance(error, errors.UserFacing) and error.code != errors.BUSY.code:
            before.cancel()
            raise error
        stale = self._stale_copy(key, held, before)
        if stale is None:
            before.cancel()
            # nothing to serve meanwhile: the read goes on off the request, with more time
            # than a request has, and keeps its page for the app's retry (cut at the
            # request's limit its work was lost, and a page needing longer never finished)
            if refresh:
                self._refresh_later(reader, key, read)
            raise error
        if refresh:
            self._refresh_later(reader, key, read)
        return stale

    def _held_good(self, key: tuple) -> Optional[tuple]:
        """The last good page of ``key``: in memory, else in the page store on disk (the
        same entry another process or an earlier run kept), within ``LAST_GOOD_SECONDS``.

        One read by other code (a deploy since: its ``code_version`` is not this one's) is
        held as ``_ReadByOtherCode``, naming every record it shows (``shown_objects``): it
        is served only once each of them, as well as each source it cites, is decided
        open for the reader by the code serving it (``_access_later``). One whose records
        cannot all be named is a miss."""
        with self._lock:
            held = self._good.get(key)
        if held is not None:
            return held
        store = getattr(getattr(self, "page_cache", None), "store", None)
        if store is None:
            return None
        from .page_store import kept_code
        found = store.get(("last-good",) + key, code=kept_code())
        if found is None:
            return None
        value = _kept_good_model(found[1])
        if not (isinstance(value, tuple) and len(value) == 4
                and isinstance(value[0], float) and isinstance(value[2], tuple)
                and hasattr(value[1], "model_copy")
                and getattr(value[1], "stale_since", None) is None):
            return None   # not a kept page of this shape: never served
        held = value[:3]
        if value[3] != store.code:
            objects = shown_objects(value[1])
            if objects is None:
                return None   # a record it shows cannot be named: never served
            held = _ReadByOtherCode(held, objects)
        with self._lock:
            if key not in self._good:
                self._bound_good()
            held = self._good.setdefault(key, held)
        return held

    @staticmethod
    def _state_of(answer: Optional[tuple]) -> Optional[tuple]:
        return None if answer is None else answer[0]

    @staticmethod
    def _access_result(future: Future, wait: float) -> Optional[tuple]:
        try:
            return future.result(timeout=wait)
        except Exception:  # noqa: BLE001 - undecidable: never matches a kept page
            future.cancel()
            return None

    def _keep_good(self, key: tuple, value: Any, before: Optional[tuple],
                   after: Optional[tuple], inputs: Any = None) -> None:
        """Keep a fresh page under the access state it was read under: the same before and
        after the read (a state that changed or could not be read keeps nothing), with the
        inputs stamp taken before its read began (None: never current)."""
        from operating.call_limits import cancelled
        if getattr(value, "stale_since", None) is not None or getattr(value, "reading", False):
            return   # never a page served stale (or one still being read) kept as fresh
        if cancelled():
            return   # read for a request dropped meanwhile: it may have stopped early
        status = getattr(value, "status", None)   # a record page's is an ObjectStatus
        if isinstance(status, str) and status in UNSERVABLE_STATUSES:
            return   # API-012: a page that could not be read is no last good page
        store = getattr(getattr(self, "page_cache", None), "store", None)
        with self._lock:
            self._good.pop(key, None)
            self._snap_inputs().pop(key, None)
            kept = before is not None and before == after
        if not kept:   # the page may not match any one access state: never served stale
            if store is not None:   # nor its disk copy, not left there for 12 h
                from .page_store import kept_code
                store.pop(("last-good",) + key, code=kept_code())
            return
        with self._lock:
            self._bound_good()
            held = self._good[key] = (time.time(), value.model_copy(deep=True), before)
            if inputs is not None:
                self._snap_inputs()[key] = inputs
        if store is not None:   # on disk too: a restart or a deploy starts from it
            from .page_store import kept_code
            store.put(("last-good",) + key,
                      (held[0], _portable_model(held[1]), held[2], store.code),
                      LAST_GOOD_SECONDS, code=kept_code())

    def _refresh_over(self, key: tuple) -> None:
        """The screen's background read ended (kept or failed): a request waiting on it
        goes on."""
        with self._lock:
            self._refreshing.discard(key)
            done = self.__dict__.setdefault("_refresh_events", {}).pop(key, None)
        if done is not None:
            done.set()

    def _refresh_now(self, reader: Reader, key: tuple, read: Callable[[], Any]) -> bool:
        """``_refresh_later`` on this thread (False: one is under way or it failed)."""
        with self._lock:
            if key in self._refreshing:
                return False
            self._refreshing.add(key)
        try:
            self._read_and_keep(reader, key, read)
            return True
        except Exception:  # noqa: BLE001 - the next request tries again
            return False
        finally:
            with self._lock:
                self._refreshing.discard(key)

    def _read_and_keep(self, reader: Reader, key: tuple, read: Callable[[], Any]) -> tuple:
        """The screen read again and kept: (the read's value, the reader's access state
        before and after it, the inputs stamp taken before it)."""
        with self._lock:   # the screen's own inputs, as its last request named them
            own = self.__dict__.get("_screen_inputs_fns", {}).get(key)
            served = self._snap_reads().get(key)
            if own is None and served and len(served) > 3:
                own = served[3]
        inputs = self._inputs_of(reader, own, key[2][0] if key[2] else None)
        before = self._access_now(reader, key[2])
        value = read()
        after = self._access_now(reader, key[2])
        self._keep_good(key, value, before, after, inputs)
        return value, before, after, inputs

    def _grants_file(self) -> Optional[Path]:
        from operating import inbox_triage as it
        return Path(self.data_dir) / it.GRANTS_FILE if self.data_dir else None

    @staticmethod
    def _flight_key(reader: Reader, key: tuple, inputs: Any) -> Optional[tuple]:
        """What a request must share with a read under way to be handed it: the screen
        and reader (``key``), the inputs it is served under (``inputs``: the store, the
        screen's own files and the reader's own writes, so a request after the reader's
        approval never gets a read begun before it) and whether the owner is viewing as
        that reader (never the same page as the reader's own). None, nothing to join on:
        no inputs (nothing is current) or inputs that cannot be compared."""
        if inputs is None:
            return None
        flight = (key, inputs, bool(getattr(getattr(reader, "session", None),
                                            "view_as", False)))
        try:
            hash(flight)
        except TypeError:
            return None
        return flight

    def _screen_read(self, key: Optional[tuple], read: Callable[[], Any],
                     here: bool) -> tuple[Future, bool]:
        """The read of ``key`` (``_flight_key``: the reader's principals and mode, the
        screen, the inputs served under and view-as) under way on another request,
        joined: (its future, False); ``key`` None is never joined nor joinable. None under way: ``read()`` is
        started, on this request's thread when ``here`` (nothing kept to serve meanwhile)
        else on its own thread, and (its future, True).

        Every request used to start its own read: when the kept page could not be served
        (no access answer within its 3 s, or none kept) each of a reader's requests for
        the same screen queued one more full read on the shared read workers, and the
        app's retry after a refusal another (11-16 walk: 27 Home requests of 1-2 browsers
        in flight at once, each refused busy after 60 s). One read per screen and reader
        now answers them all; the same principals and mode read the same page, as a kept
        page is shared between them already."""
        with self._lock:
            flights = self.__dict__.setdefault("_screen_reads", {})
            box = flights.get(key) if key is not None else None
            if box is not None:
                return box, False
            box = Future()
            if key is not None:   # None: a read of its own that nobody joins
                flights[key] = box

        def run() -> None:
            from operating.call_limits import cancel_scope, current_cancel
            try:
                # a read others may join (and that keeps its page for the next request)
                # is no one request's: the one that started it leaving never cuts it
                with cancel_scope(None if key is not None else current_cancel()):
                    box.set_result(read())
            except BaseException as error:  # noqa: BLE001 - handed to the requests
                box.set_exception(error)
            finally:
                with self._lock:
                    if flights.get(key) is box:
                        del flights[key]

        if here:
            run()   # nothing to serve meanwhile: read on the request's own thread
        else:
            threading.Thread(target=run, name="serving-read", daemon=True).start()
        return box, True
