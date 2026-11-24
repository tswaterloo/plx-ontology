"""The keyed, parameterized store reads behind ``StoreReader``
(``operating/ontology_tools.py``), which adds the identifier prefix/suffix reads."""

from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from ontology.store import format_problem, removals_at, withhold_currency
from ontology.text_index import chunk_text
from operating.ontology_tools_policy import (_citation_pairs, _CITATION_RANGE, _readable_text,
                                             _SNAPSHOT, _then, _visible_object_sql,
                                             AUDIENCE_KEYS_SQL, HIT_FIELD_CHUNKS, HIT_FIELDS,
                                             NARROWED_ROWS_PER_ID, TYPE_AUDIENCE_SQL)

if TYPE_CHECKING:
    from operating.ontology_tools_policy import _Scope


class StoreReads:
    """Read-only queries over a ``plx-ontology-store/v10`` file (see ``StoreReader``)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        # One sqlite3 connection is shared by every tool on this store (L8 and L9);
        # each tool call holds this lock so calls from several threads never
        # interleave on the connection.
        self.lock = threading.RLock()
        # Reader-independent facts of the store as this connection last saw it, shared by
        # every L9 table over this reader (``TraverseTools._store_cached``).
        self.facts: tuple[tuple[int, int], dict[Any, Any]] | None = None
        self._audiences: tuple[tuple[int, int], tuple[str, ...]] | None = None
        self._meta: tuple[tuple[int, int], dict[str, str]] | None = None
        self._text_schemes: tuple[tuple[int, int], dict[str, bool]] | None = None

    @contextmanager
    def word_rows(self, table: str, words: str,
                  spans: Sequence[tuple[int, int]] | None = None, *,
                  keep: Callable[[int], bool] | None = None) -> Iterator[Iterator[int]]:
        """The row numbers of ``table``'s word index (``value_text``: value rows,
        ``chunk_text``: chunk ids) matching the FTS5 query ``words``, ascending, within
        ``spans`` (inclusive row number ranges) when given, and only those ``keep``
        admits; read lazily as they are taken (a common word is read only as far as the
        caller goes). ``sqlite3.OperationalError``: a match the index cannot read."""
        conn = self.chunk_conn if table == "chunk_text" else self.word_conn
        narrowed = "" if spans is None else " AND (" + " OR ".join(
            f"rowid BETWEEN {int(low)} AND {int(high)}" for low, high in spans) + ")"
        # Postgres: a server-side cursor read a batch at a time (``stream``)
        read = getattr(conn, "stream", conn.execute)
        cursor = read(f"SELECT rowid FROM {table} WHERE {table} MATCH :words"
                      f"{narrowed} ORDER BY rowid", {"words": words})

        def ids() -> Iterator[int]:
            while True:
                page = cursor.fetchmany(self.WORD_ROWS_BATCH)
                if not page:
                    return
                for (rowid,) in page:
                    if keep is None or keep(rowid):
                        yield rowid
        try:
            yield ids()
        finally:
            cursor.close()

    @property
    def chunk_conn(self) -> sqlite3.Connection:
        """The connection document words (``chunk_text``) are read on: this store's
        (``PgStoreReader``: the SQLite generation's)."""
        return self.word_conn

    @property
    def word_conn(self) -> sqlite3.Connection:
        """The connection the word index (``value_text``, ``chunk_text``) is read on: this
        store's (``operating.pg.ontology_reader.PgStoreReader``: the SQLite generation's)."""
        return self.conn

    @classmethod
    def open(cls, path: str | Path) -> "StoreReader":
        uri = Path(path).resolve().as_uri() + "?mode=ro"
        return cls(sqlite3.connect(uri, uri=True, check_same_thread=False))

    @contextmanager
    def one_version(self) -> Iterator[None]:
        """One tool call on this thread: the store's data version (``data_version``) is
        polled once and that answer used for the call's every stamp (a call reads one
        store state; a timeline call polled it ten times). A call inside one shares it."""
        local = self.__dict__.setdefault("_version_pin", threading.local())
        if getattr(local, "box", None) is not None:
            yield
            return
        local.box = []
        try:
            yield
        finally:
            local.box = None

    def data_version(self) -> Any:
        """``PRAGMA data_version`` (Postgres: the data version loads and delta layers
        move), polled once per tool call inside ``one_version``."""
        local = self.__dict__.get("_version_pin")
        box = getattr(local, "box", None) if local is not None else None
        if box:
            return box[0]
        version = self.conn.execute("PRAGMA data_version").fetchone()[0]
        if box is not None:
            box.append(version)
        return version

    def meta(self, _version: Any = None) -> dict[str, str]:
        """The store's ``store_meta`` rows, kept until the store changes (the stamp of
        ``audience_keys``): every tool call reads them, the build report among them.
        ``_version``: the data version the caller read just now (one poll, not two)."""
        with self.lock:
            version = self.data_version() if _version is None else _version
            stamp = (version, self.conn.total_changes)
            if self._meta is None or self._meta[0] != stamp:
                self._meta = (stamp, {row["key"]: row["value"] for row in
                                      self.conn.execute("SELECT key, value FROM store_meta")})
            return dict(self._meta[1])

    def change_stamp(self) -> tuple[Any, ...]:
        """What changes whenever the store this reader reads does: its connection, a commit
        on another connection (``PRAGMA data_version``; Postgres: the data version a load
        or a delta layer moves), a write on this one, and the generation's content digest.
        A value read before under the same stamp is still the store's."""
        with self.lock:
            version = self.data_version()
            return (id(self.conn), version, self.conn.total_changes,
                    self.meta(version).get("content_digest"))

    def audience_keys(self) -> list[str]:
        """Every audience key of the store's objects (no reader's grants applied), kept
        until the store changes: a commit on another connection (``PRAGMA data_version``)
        or a write on this one (``total_changes``), the stamp of ``facts``. On Postgres read
        by the audience index, one step per key (``AUDIENCE_KEYS_SQL``), never a scan of
        every object; a SQLite store has no such index (each step would scan), so it is
        one scan there."""
        with self.lock:
            stamp = (self.data_version(), self.conn.total_changes)
            if self._audiences is None or self._audiences[0] != stamp:
                self._audiences = (stamp, tuple(row[0] for row in self.conn.execute(
                    AUDIENCE_KEYS_SQL if getattr(self.conn, "postgres", False)
                    else "SELECT DISTINCT audience_key FROM objects ORDER BY audience_key")))
            return list(self._audiences[1])

    def type_audience_keys(self) -> list[tuple[str, str]]:
        """Every (object type, audience key) pair of the store's objects, kept until the
        store changes (as ``audience_keys``). On Postgres read by the type and audience
        index, one step per pair (``TYPE_AUDIENCE_SQL``: 0.2 s over 13,800 pairs), never a
        scan of every object (10 s, and a wait for a new store state's facts); a database
        without that index (``objects_by_type_audience``) is scanned as before."""
        with self.lock:
            stamp = (self.data_version(), self.conn.total_changes)
            held = getattr(self, "_type_audiences", None)
            if held is None or held[0] != stamp:
                # the skip-scan only where its index is (built by hand on the served
                # database, in no migration): without it each step sorts a whole type
                indexed = getattr(self.conn, "postgres", False) and self.conn.execute(
                    "SELECT to_regclass('ontology.objects_by_type_audience') IS NOT NULL"
                ).fetchone()[0]
                held = self._type_audiences = (stamp, tuple(
                    (row[0], row[1]) for row in self.conn.execute(
                        TYPE_AUDIENCE_SQL if indexed
                        else "SELECT object_type, audience_key FROM objects GROUP BY 1, 2")))
            return list(held[1])

    def remember_audience_keys(self, keys: Sequence[str]) -> None:
        """``audience_keys`` as read before from this unchanged store (the facts a host
        keeps beside the file, ``ontology_tools_traverse.prime``), kept as if read now."""
        with self.lock:
            stamp = (self.data_version(), self.conn.total_changes)
            self._audiences = (stamp, tuple(keys))

    @staticmethod
    def _at(alias: str) -> str:
        """``alias`` row current at :at. julianday compares ISO times at
        millisecond resolution; an unparseable time is NULL and never matches."""
        return (f"julianday({alias}.recorded_at) <= julianday(:at) AND "
                f"({alias}.superseded_at IS NULL OR julianday({alias}.superseded_at) > julianday(:at))")

    @property
    def _VISIBLE_OBJECT(self) -> str:  # noqa: N802 - the predicate every object read uses
        """The store's visibility, and not a document whose search chunks carry a marking
        the reader lacks (a pay stub: ``operating.search.document_marks``)."""
        return _visible_object_sql(self._VISIBLE_OBJECT_STORE)

    def _scoped_identifiers(self) -> str:
        return ("SELECT i.*, o.object_type, o.binding_id, o.audience_key "
                "FROM identifiers i JOIN objects o ON o.object_id = i.object_id "
                "WHERE " + self._VISIBLE_OBJECT + "AND " + self._at("i") + " "
                "AND i.markings IN (SELECT value FROM json_each(:marks)) ")

    def identifiers_by_keys(self, scope: _Scope, keys: Sequence[tuple[str, str]]
                            ) -> list[sqlite3.Row]:
        if not keys:
            return []
        return list(self.conn.execute(
            self._scoped_identifiers() + "AND (i.identifier_kind, i.normalized) IN "
            "(SELECT json_extract(value, '$[0]'), json_extract(value, '$[1]') "
            "FROM json_each(:keys))", scope.params(keys=json.dumps([list(k) for k in keys]))))

    def identifiers_by_value(self, scope: _Scope, values: Sequence[str],
                             types: Sequence[str] | None = None,
                             cap: int | None = None) -> list[sqlite3.Row]:
        """Readable identifiers whose canonical form is one of ``values`` (the index on
        ``normalized``). ``types``: of those object types only, filtered here, before
        ``cap`` cuts the rows, in a set order (type, object, property): a short number many
        records of other types carry ("465") never crowds out the types asked."""
        sql = self._scoped_identifiers() + "AND i.normalized IN (SELECT value FROM json_each(:v)) "
        params: dict[str, Any] = {"v": json.dumps(list(values))}
        if types is not None:
            # on the identifier row itself (an object id starts with its type), one
            # parameter per type: Postgres drops other types' rows in the index scan,
            # before the per-row visibility checks (a common number, "1", carried by
            # 53,774 identifiers: 2.2 s as a join filter, 17 ms here; EXPLAIN 11-11)
            wanted = sorted(set(types))
            if not wanted:
                return []
            sql += ("AND substr(i.object_id, 1, instr(i.object_id, ':') - 1) IN ("
                    + ", ".join(f":t{n}" for n in range(len(wanted))) + ") ")
            params.update({f"t{n}": t for n, t in enumerate(wanted)})
        if cap is not None:
            sql += "ORDER BY o.object_type, i.object_id, i.property, i.normalized LIMIT :cap"
            params["cap"] = int(cap)
        return list(self.conn.execute(sql, scope.params(**params)))

    def primary_identifiers(self, scope: _Scope, object_ids: Sequence[str]) -> list[sqlite3.Row]:
        """The primary identifiers this scope may read of ``object_ids``, each object's in
        (kind, normalized) order; inside ``snapshot_reads`` each object's are read once
        for the same query inputs (``visible_versions``' rule)."""
        snap = _SNAPSHOT.get()
        if snap is None:
            return self._primary_identifiers_read(scope, object_ids)
        params = scope.params()
        memo = snap.setdefault("identifiers", {}).setdefault(
            (id(self), tuple(sorted((k, str(v)) for k, v in params.items()))), {})
        wanted = [oid for oid in dict.fromkeys(object_ids) if oid not in memo]
        if wanted:
            found: dict[str, list[sqlite3.Row]] = {oid: [] for oid in wanted}
            for row in self._primary_identifiers_read(scope, wanted):
                found[row["object_id"]].append(row)
            memo.update(found)
        return [row for oid in dict.fromkeys(object_ids) for row in memo[oid]]

    def _primary_identifiers_read(self, scope: _Scope, object_ids: Sequence[str]
                                  ) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            self._scoped_identifiers() + "AND i.role = 'primary' "
            "AND i.object_id IN (SELECT value FROM json_each(:ids)) "
            "ORDER BY i.object_id, i.identifier_kind, i.normalized",
            scope.params(ids=json.dumps(list(object_ids)))))

    def _text_layouts(self, properties: Sequence[str] | None) -> list[int]:
        """The value layouts a text pass can match: those holding one of ``properties``
        (None: those holding a text value). A cell outside them never passes the pass's
        own property or type test, so this is a necessary condition, never a new one."""
        if properties is None:
            where, params = "k.value_type = 'text'", {}
        else:
            where, params = ("vf.property IN (SELECT value FROM json_each(:props))",
                             {"props": json.dumps(sorted(set(properties)))})
        return [row[0] for row in self.conn.execute(
            "SELECT DISTINCT lf.layout FROM value_layout_fields lf "
            "JOIN value_fields vf ON vf.field = lf.field JOIN value_kinds k ON k.kind = lf.kind "
            "WHERE " + where + " ORDER BY lf.layout", params)]

    def _layout_candidates(self, words: str, layouts: Sequence[int], after: int
                           ) -> Iterator[list[int]] | None:
        """The value rows past ``after`` the word index matches whose cell has one of
        ``layouts``, in row order, tested against the layouts before any value, object
        or permission row is joined; None when the store cannot read it that way.

        Read lazily, one window of word matches at a time (``TEXT_LAYOUT_NARROW_ROWS``
        of them, doubling up to ``TEXT_LAYOUT_WINDOW_MAX``), each tested as one set
        against its cells' layouts by row number, so a caller that fills its page from
        the first rows stops there. Intersecting the layouts' whole index ranges with
        every word match instead read every cell of those layouts on every page: a
        broad "purchase order" read 79,000 candidates over 118 layouts (1.5 to 6 s) per
        page of 401, while one window's test reads 5,000 cells by row number (10 to
        20 ms warm). The windows are consecutive and each is sorted, so their
        concatenation is the whole candidate list in row order."""
        matches = self._word_windows(words, after)
        try:
            # A word matching few rows is one window, read once: handing the plain scan
            # its rows to read again cost a common prefix's merge (3 s) twice.
            hits = next(matches, [])
            windows = self._layout_windows(json.dumps(list(layouts)), hits, matches)
            first = next(windows, [])
        except sqlite3.OperationalError:
            matches.close()
            return None   # a store that cannot read it so: the word index drives the pass
        return _then(first, windows)

    def holds_text_of(self, schemes: Sequence[str]) -> bool:
        """Whether the document-text index holds a field cited under one of ``schemes``
        (``graph-mail``: a citation ``graph-mail://...``): one index seek per scheme (the
        ``text_fields`` key leads with its citation), kept until the store changes. A
        store without the index holds none; one that cannot be read says True (the word
        pass then runs, as before)."""
        with self.lock:
            try:
                stamp = (self.data_version(), self.conn.total_changes)
                if self._text_schemes is None or self._text_schemes[0] != stamp:
                    self._text_schemes = (stamp, {})
                held = self._text_schemes[1]
                for scheme in schemes:
                    if scheme not in held:
                        # every citation of the scheme sorts in [scheme://, scheme:/0)
                        held[scheme] = self.conn.execute(
                            "SELECT 1 FROM text_fields WHERE citation >= ? AND citation < ? "
                            "LIMIT 1", (f"{scheme}://", f"{scheme}:/0")).fetchone() is not None
                    if held[scheme]:
                        return True
                return False
            except sqlite3.OperationalError as error:
                if "no such table" in str(error):
                    return False
                return True

    def chunk_matches(self, scope: _Scope, words: str, object_type: str | None, after: int,
                      batch: int) -> list[sqlite3.Row]:
        """Readable chunks of captured document text (``ontology/text_index.py``) holding
        every word of ``words``, keyset-paged by chunk id: the object readable, the
        version the field reads current at :at, the field's markings permitted (all in
        SQL; the caller re-checks the item ACL through that version)."""
        where = "AND o.object_type = :type " if object_type is not None else ""
        return _readable_text(self, scope, self.conn.execute(
            "SELECT t.chunk_id AS row_no, t.body, fd.object_id, fd.version_handle, "
            "fd.citation, fd.field_path, fd.sender_class, o.object_type, "
            "o.binding_id AS object_binding, o.audience_key FROM chunk_text f "
            "JOIN text_chunks t ON t.chunk_id = f.rowid "
            "JOIN text_fields fd ON fd.field_id = t.field_id "
            "JOIN objects o ON o.object_id = fd.object_id "
            "JOIN object_versions v ON v.l1_handle = fd.version_handle "
            "AND v.object_id = fd.object_id "
            "WHERE chunk_text MATCH :words AND f.rowid > :after AND " + self._VISIBLE_OBJECT
            + "AND " + self._at("v") + " AND fd.markings IN (SELECT value FROM json_each(:marks)) "
            + where + "ORDER BY f.rowid LIMIT :batch",
            scope.params(words=words, after=after, batch=batch, type=object_type)))

    def sensitive_versions(self, pairs: Sequence[tuple[str, str]]) -> set[str]:
        """The version handles among ``pairs`` ((object id, version handle)) whose document
        is personal financial or HR-confidential (``document_marks.sensitive_fields``):
        each version judged once on every chunk of its text fields, in order, and its
        name, subject, path and label values, whoever reads; the verdict kept."""
        from ontology.text_index import chunk_text
        from operating.search import document_marks
        kept = self.__dict__.setdefault("_sensitive_kept", {})
        todo = sorted({(o, h) for o, h in pairs if h not in kept})
        for start in range(0, len(todo), 200):
            part = dict(todo[start:start + 200])
            pairs_json = _citation_pairs(part)
            fields: dict[str, list[tuple[str, str]]] = {h: [] for h in part.values()}
            for handle, path, body in self.conn.execute(
                    "SELECT fd.version_handle, fd.field_path, t.body FROM json_each(:pairs) p "
                    "CROSS JOIN text_fields fd ON " + _CITATION_RANGE + " "
                    "JOIN text_chunks t ON t.field_id = fd.field_id "
                    "ORDER BY fd.version_handle, fd.field_id, t.start", {"pairs": pairs_json}):
                fields.setdefault(handle, []).append((path, chunk_text(body)))
            for handle, path, value in self.conn.execute(
                    "SELECT v.l1_handle, v.field_path, v.value FROM json_each(:pairs) p "
                    "JOIN object_values v ON v.object_id = json_extract(p.value, '$[0]') "
                    "AND v.l1_handle = json_extract(p.value, '$[1]') WHERE v.value_type = "
                    "'text' AND v.field_path IN (SELECT value FROM json_each(:cues))",
                    {"pairs": pairs_json, "cues": json.dumps(document_marks.CUE_PATHS)}):
                fields.setdefault(handle, []).append((path, value))
            if len(kept) > self.SENSITIVE_KEPT:
                kept.clear()
            for handle, got in fields.items():
                kept[handle] = document_marks.sensitive_fields(got)
        return {h for _o, h in pairs if kept.get(h)}

    def object_chunks(self, scope: _Scope, versions: Mapping[str, str],
                      hints: Mapping[str, Sequence[str]] | None = None) -> list[sqlite3.Row]:
        """Readable chunks of captured document text of each object of ``versions``
        (object_id -> the l1 handle of the version read; the shape of ``chunk_matches``,
        no word match): the object readable, that version current at :at, the field's
        markings permitted. Never every chunk of an object (AUD-11: a free-text find read
        every chunk of every hit, 205 s): ``hints`` (object_id -> the citations of the
        fields its best-ranked search chunks were cut from, best first) are read first,
        the first ``HIT_FIELD_CHUNKS`` chunks of each such field; an object none of whose
        hinted fields this store holds for that version (or with no hint) is read from
        its first ``HIT_FIELDS`` readable fields in citation order. Rows come per object,
        hinted fields in hint order, then in field and chunk order.

        A field's citation always begins with its version's handle (a page or member adds
        a suffix), so each version is one seek on the ``text_fields`` key, which leads
        with the citation, and each field's chunks one seek on ``(field_id, start)``."""
        hinted: list[Any] = []
        if hints:
            triples = [[oid, versions[oid], citation, rank] for oid in versions
                       for rank, citation in enumerate(dict.fromkeys(hints.get(oid) or ()))
                       if isinstance(citation, str)
                       and citation.startswith(versions[oid])]
            if triples:
                hinted = list(self.conn.execute(
                    self._CHUNK_COLUMNS + "FROM json_each(:hints) p "
                    "CROSS JOIN text_fields fd ON fd.citation = json_extract(p.value, '$[2]') "
                    "AND fd.version_handle = json_extract(p.value, '$[1]') "
                    "AND fd.object_id = json_extract(p.value, '$[0]') "
                    + self._CHUNK_JOINS + "ORDER BY fd.object_id, json_extract(p.value, "
                    "'$[3]'), fd.field_id, t.start",
                    scope.params(hints=json.dumps(triples), per_field=HIT_FIELD_CHUNKS)))
        held = {row["object_id"] for row in hinted}
        rest = {oid: handle for oid, handle in versions.items() if oid not in held}
        first: list[Any] = []
        if rest:
            first = list(self.conn.execute(
                self._CHUNK_COLUMNS + "FROM json_each(:pairs) p "
                "CROSS JOIN text_fields fd ON " + _CITATION_RANGE + " "
                + self._CHUNK_JOINS + "AND fd.field_id IN (SELECT f2.field_id FROM "
                "text_fields f2 WHERE f2.citation >= json_extract(p.value, '$[1]') "
                "AND f2.citation < json_extract(p.value, '$[2]') "
                "AND f2.version_handle = json_extract(p.value, '$[1]') "
                "AND f2.object_id = json_extract(p.value, '$[0]') "
                "AND f2.markings IN (SELECT value FROM json_each(:marks)) "
                "ORDER BY f2.citation, f2.field_path LIMIT :fields) "
                "ORDER BY fd.object_id, fd.field_id, t.start",
                scope.params(pairs=_citation_pairs(rest), per_field=HIT_FIELD_CHUNKS,
                             fields=HIT_FIELDS)))
        return _readable_text(self, scope, hinted + first)

    @property
    def _CHUNK_JOINS(self) -> str:   # noqa: N802 - a statement part, as _VISIBLE_OBJECT
        return ("JOIN text_chunks t ON t.field_id = fd.field_id "
                "JOIN objects o ON o.object_id = fd.object_id "
                "JOIN object_versions v ON v.l1_handle = fd.version_handle "
                "AND v.object_id = fd.object_id "
                "WHERE " + self._VISIBLE_OBJECT + "AND " + self._at("v")
                + " AND fd.markings IN (SELECT value FROM json_each(:marks)) "
                "AND t.chunk_id IN (SELECT t2.chunk_id FROM text_chunks t2 "
                "WHERE t2.field_id = fd.field_id ORDER BY t2.start LIMIT :per_field) ")

    def objects_of_type(self, scope: _Scope, object_type: str, after: str,
                        batch: int, narrowing: Sequence[tuple[str, dict[str, Any]]] = (),
                        among: Sequence[str] | None = None,
                        events: Sequence[tuple[str, str, str | None]] = (),
                        upto: str | None = None, *, versions: bool = False
                        ) -> list[sqlite3.Row]:
        # ``versions``: each row is the object's version current at :at with its audience
        # key (``visible_versions``'s rows), read in the same statement, so a scan never
        # reads its batch's versions again (11-10: a count over 76k sales orders spent
        # 10 s of 56 s re-reading what the scan had just decided).
        # An object id begins with its type ("<Type>:..."): the type's ids are one range
        # of the object key, walked in key order and stopped at the batch, so the
        # visibility predicate is read for the rows taken, never for every object of the
        # type before a sort (77k objects of one type: 0.45 s a batch, now milliseconds).
        # The type is still compared, on its value only (``|| ''``: no index on it).
        # ``narrowing`` (``_value_narrowing``): per filter, a condition on one stored
        # value of the object that every object the filter can match or leave undecided
        # meets; only objects meeting each are read, before their visibility (11-06
        # q18: a date range over 77k sales orders read every one, 28-52 s a call).
        narrow, extra = "", {}
        for n, (condition, values) in enumerate(narrowing):
            narrow += ("EXISTS (SELECT 1 FROM object_values x WHERE x.object_id = o.object_id "
                       f"AND x.property = :np{n} AND ((" + condition.replace(":n_", f":n{n}_")
                       + ") OR x.markings NOT IN (SELECT value FROM json_each(:marks)))) AND ")
            extra[f"np{n}"] = values["property"]
            extra.update({f"n{n}_{k}": v for k, v in values.items() if k != "property"})
        for n, (event_type, op, day) in enumerate(events):
            # a filter on the type's event: only records holding one of its times
            bound = "" if op not in ("gte", "gt", "lt", "lte") or day is None else (
                f"AND e.time {'>=' if op in ('gte', 'gt') else '<'} :ed{n} ")
            narrow += ("EXISTS (SELECT 1 FROM event_objects eo JOIN events e ON e.event_id = "
                       f"eo.event_id WHERE eo.object_id = o.object_id AND e.event_type = :et{n} "
                       + bound + ") AND ")
            extra[f"et{n}"] = event_type
            if bound:
                extra[f"ed{n}"] = day
        if upto is not None:
            narrow += "o.object_id <= :upto AND "
            extra["upto"] = upto
        if among is not None:
            # only these records (a link filter's): their ids, read on the object key
            narrow += "o.object_id IN (SELECT value FROM json_each(:among)) AND "
            extra["among"] = json.dumps(list(among))
        head = ("SELECT v.*, o.audience_key FROM objects o JOIN object_versions v "
                "ON v.object_id = o.object_id AND " + self._at("v") + " ") if versions \
            else "SELECT o.* FROM objects o "
        return list(self.conn.execute(
            head + "WHERE o.object_type || '' = :type "
            "AND o.object_id > :after AND o.object_id >= :lo AND o.object_id < :hi AND "
            + narrow + self._VISIBLE_OBJECT + "ORDER BY o.object_id LIMIT :batch",
            scope.params(type=object_type, after=after, batch=batch, lo=object_type + ":",
                         hi=object_type + ";", **extra)))

    def narrowed_ids(self, object_type: str,
                     narrowing: Sequence[tuple[str, dict[str, Any]]], marks: str
                     ) -> list[str] | None:
        """The ids of ``object_type`` meeting every condition of ``narrowing`` (each the
        very ``EXISTS`` condition ``objects_of_type`` applies, the withheld-markings
        clause included, so the set is the one its span scan keeps), read from the
        values of each narrowed property in one statement per condition rather than one
        per span of keys (11-09: a file name over 190k SharePoint files, 95 statements and
        31-49 s, against one of 4 s). A condition holding more than ``NARROWED_IDS_CAP``
        records narrows nothing and is left to the scan, which applies every filter to the
        candidates anyway (a week's dates: "on or after the Monday" holds a few hundred
        orders, "before the next Monday" every order ever; 11-10, the weekly review counted
        each week by a span scan of all 84k order dates). None when the store does not read
        values first or no condition is selective: the caller scans spans."""
        if not self.VALUES_FIRST or not narrowing:
            return None
        found: set[str] | None = None
        for condition, values in narrowing:
            params = {k: v for k, v in values.items() if k != "property"}
            # value rows, not distinct ids: a broad condition stops at the row limit at
            # once (a record holds a value per version, so the limit is a few per id)
            limit = self.NARROWED_IDS_CAP * NARROWED_ROWS_PER_ID
            rows = self.conn.execute(
                "SELECT x.object_id FROM object_values x WHERE x.property = :np "
                "AND x.object_id >= :lo AND x.object_id < :hi AND ((" + condition + ") OR "
                "x.markings NOT IN (SELECT value FROM json_each(:marks))) LIMIT :cap",
                {"np": values["property"], "lo": object_type + ":", "hi": object_type + ";",
                 "marks": marks, "cap": limit + 1,
                 **{f"n_{k}": v for k, v in params.items()}}).fetchall()
            ids = {row[0] for row in rows}
            if len(rows) > limit or len(ids) > self.NARROWED_IDS_CAP:
                continue   # not selective: a superset either way, the scan decides
            found = ids if found is None else found & ids
        return None if found is None else sorted(found)

    def audience_of_version(self, handle: str) -> str | None:
        """The audience key of the record whose version ``handle`` is (``version_audience``),
        None when none decides. Names no value."""
        found = self.version_audience(handle)
        return None if found is None else found[1]

    def version_audience(self, handle: str) -> tuple[frozenset[str], str] | None:
        """(the objects, their one audience key) of the version ``handle`` (one seek on the
        versions' handle key). A version the projection does not hold (read live, newer
        than the projection's: ``<record>@<revision>``) takes its record's: the audience
        every held version of that record (the handles before its ``@``) is kept under,
        when it is one. None when neither decides. Names no value."""
        with self.lock:
            rows = self.conn.execute(
                "SELECT o.object_id, o.audience_key FROM object_versions v JOIN objects o "
                "ON o.object_id = v.object_id WHERE v.l1_handle = ? LIMIT 1",
                (handle,)).fetchall()
            if not rows and "@" in handle:
                record = handle.rsplit("@", 1)[0]
                # the record's versions: one range of the handle key ("@" < "A")
                rows = self.conn.execute(
                    "SELECT DISTINCT o.object_id, o.audience_key FROM object_versions v "
                    "JOIN objects o ON o.object_id = v.object_id WHERE v.l1_handle >= ? "
                    "AND v.l1_handle < ? LIMIT 50", (record + "@", record + "A")).fetchall()
        keys = {row[1] for row in rows}
        if len(keys) != 1:
            return None
        return frozenset(row[0] for row in rows), next(iter(keys))

    def visible_versions(self, scope: _Scope, object_ids: Sequence[str]) -> list[sqlite3.Row]:
        """The version current at :at of each readable object among ``object_ids``; inside
        ``snapshot_reads`` each object's answer for the same query inputs is kept."""
        snap = _SNAPSHOT.get()
        if snap is None:
            return self._visible_versions_read(scope, object_ids)
        params = scope.params()
        memo = snap["versions"].setdefault(
            (id(self), tuple(sorted((k, str(v)) for k, v in params.items()))), {})
        wanted = [oid for oid in dict.fromkeys(object_ids) if oid not in memo]
        if wanted:
            rows = self._visible_versions_read(scope, wanted)
            found = {row["object_id"]: row for row in rows}
            for oid in wanted:
                memo[oid] = found.get(oid)
        return [memo[oid] for oid in dict.fromkeys(object_ids) if memo.get(oid) is not None]

    def _visible_versions_read(self, scope: _Scope, object_ids: Sequence[str]
                               ) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT v.*, o.audience_key FROM object_versions v "
            "JOIN objects o ON o.object_id = v.object_id "
            "WHERE o.object_id IN (SELECT value FROM json_each(:ids)) AND " + self._VISIBLE_OBJECT
            + "AND " + self._at("v"), scope.params(ids=json.dumps(list(object_ids)))))

    def readable_values(self, scope: _Scope, object_ids: Sequence[str],
                        properties: Sequence[str] | None = None) -> list[sqlite3.Row]:
        """Values current at :at with permitted markings (the caller passes only
        readable objects), each currency as this reader may read it (``scoped_currency``);
        of ``properties`` only when named (None = all)."""
        where = ("AND v.property IN (SELECT value FROM json_each(:props)) "
                 if properties is not None else "")
        return self.scoped_currency(scope, self.conn.execute(
            "SELECT * FROM object_values v WHERE v.object_id IN (SELECT value FROM json_each(:ids)) "
            + where + "AND " + self._at("v") + " AND v.markings IN (SELECT value FROM json_each(:marks)) "
            "ORDER BY v.object_id, v.property, v.authority_rank IS NULL, v.authority_rank, "
            "v.l1_handle, v.field_path", scope.params(ids=json.dumps(list(object_ids)),
                                                       props=json.dumps(list(properties or ())))))

    def key_ahead(self, object_type: str, after: str, span: int) -> str | None:
        """The ``span``-th key of ``object_type`` after ``after`` (the object key alone,
        no visibility); None when fewer remain."""
        row = self.conn.execute(
            "SELECT object_id FROM objects WHERE object_id > :after AND object_id >= :lo "
            "AND object_id < :hi ORDER BY object_id LIMIT 1 OFFSET :skip",
            {"after": after, "lo": object_type + ":", "hi": object_type + ";",
             "skip": span - 1}).fetchone()
        return row[0] if row else None

    def event_times(self, scope: _Scope, object_ids: Sequence[str],
                    event_types: Sequence[str]) -> list[sqlite3.Row]:
        """(object_id, event_type, time, readable) of each event of ``event_types``
        relating to these objects, current at :at; ``readable`` 0 when the reader does
        not hold its markings (a time it may not read, never one it lacks)."""
        return list(self.conn.execute(
            "SELECT eo.object_id, e.event_type, e.time, CASE WHEN e.markings IN "
            "(SELECT value FROM json_each(:marks)) THEN 1 ELSE 0 END AS readable "
            "FROM event_objects eo JOIN events e ON e.event_id = eo.event_id "
            "WHERE eo.object_id IN (SELECT value FROM json_each(:ids)) "
            "AND e.event_type IN (SELECT value FROM json_each(:types)) AND e.time IS NOT NULL "
            "AND " + self._at("e") + " ORDER BY eo.object_id",
            scope.params(ids=json.dumps(list(object_ids)), types=json.dumps(list(event_types)))))

    def withheld_values(self, scope: _Scope, object_ids: Sequence[str],
                        properties: Sequence[str] | None = None) -> list[sqlite3.Row]:
        """(object_id, property) of the values current at :at among these objects (and
        properties; None = all) whose markings the reader does not hold: what
        ``readable_values`` leaves out, which a caller must never read as no value."""
        where = ("AND v.property IN (SELECT value FROM json_each(:props)) "
                 if properties is not None else "")
        return list(self.conn.execute(
            "SELECT DISTINCT v.object_id, v.property FROM object_values v WHERE v.object_id IN "
            "(SELECT value FROM json_each(:ids)) " + where + "AND " + self._at("v")
            + " AND v.markings NOT IN (SELECT value FROM json_each(:marks)) "
            "ORDER BY v.object_id, v.property",
            scope.params(ids=json.dumps(list(object_ids)),
                         props=json.dumps(list(properties or ())))))

    def values_by_citation(self, pairs: Sequence[tuple[str, str]]) -> list[sqlite3.Row]:
        """Every stored value (of any object) at one of the cited (L1 handle, field
        path) cells. A value's version is one of its object's versions (foreign key), so
        the handle index of ``object_versions`` names the objects to read; the values
        carry no handle index, and a read by handle alone scans every value."""
        if not pairs:
            return []
        return list(self.conn.execute(
            "SELECT * FROM object_values WHERE object_id IN (SELECT object_id FROM "
            "object_versions WHERE l1_handle IN (SELECT json_extract(value, '$[0]') "
            "FROM json_each(:pairs))) AND (l1_handle, field_path) IN "
            "(SELECT json_extract(value, '$[0]'), json_extract(value, '$[1]') "
            "FROM json_each(:pairs))", {"pairs": json.dumps([list(p) for p in pairs])}))

    def format_problem(self) -> str | None:
        """Why this code cannot read the store (``ontology.store.format_problem``, the
        check ``ProjectionStore`` refuses on): it names another store format (an older
        projection a host serves until its next build) or none, so it must be rebuilt;
        else None."""
        return format_problem(self.conn)

    def scoped_currency(self, scope: _Scope, rows: Iterable[Any]) -> list[Any]:
        """``rows`` with a currency read on another record kept only where this reader may
        read that record at :at (``ontology.store.withhold_currency``)."""
        return withhold_currency(rows, lambda ids: (
            row["object_id"] for row in self.visible_versions(scope, ids)))

    def removals(self, object_ids: Sequence[str], at: str
                 ) -> dict[str, tuple[str, str | None]]:
        """(basis, since) of the removal in force at ``at`` of each of these records that
        has one (``ontology.store.removals_at``: the one removal test). Callers pass ids
        the reader may read."""
        return removals_at(self.conn, object_ids, at)

    def versions_of(self, object_id: str) -> list[sqlite3.Row]:
        """Every captured version of one record, oldest first (no visibility: the caller,
        ``ontology_tools_history``, judges each version for its reader)."""
        return list(self.conn.execute(
            "SELECT object_id, l1_handle, lineage, markings, recorded_at, superseded_at "
            "FROM object_versions WHERE object_id = ? ORDER BY recorded_at", (object_id,)))

    def removal_spans(self, object_id: str) -> list[tuple[str, str | None, str | None]]:
        """(basis, since, until) of each stretch the record's source did not hold it
        (``ontology.store.removals_at``'s rows: its removals, else its missing mark)."""
        return [(row[0], row[1], row[2]) for row in self.conn.execute(
            "SELECT COALESCE(rm.basis, 'missing_from_source'), "
            "COALESCE(rm.since, mo.missing_from_source_since), rm.until FROM objects mo "
            "LEFT JOIN object_removals rm ON rm.object_id = mo.object_id "
            "WHERE mo.object_id = ? AND COALESCE(rm.since, mo.missing_from_source_since) "
            "IS NOT NULL", (object_id,))]

    def link_history(self, scope: _Scope, object_id: str, column: str, after: Any,
                     rank: int, limit: int) -> list[sqlite3.Row]:
        """Up to ``limit`` links with an end at ``object_id`` whose ``column``
        (recorded_at: added, superseded_at: removed) is at or before the scope's time and
        before the history position ``after`` (its time, then rank, then link id; None:
        from the newest), newest first, with markings this reader holds. The other end
        and the tier's reader view are the caller's to judge."""
        if column not in ("recorded_at", "superseded_at"):
            raise ValueError(column)
        where = ""
        params = scope.params(id=object_id, limit=limit)
        if after is not None:
            params.update(t=after.text, lid=after.key)
            if rank < after.rank:
                where = f"AND julianday(l.{column}) <= julianday(:t) "
            elif rank > after.rank:
                where = f"AND julianday(l.{column}) < julianday(:t) "
            else:
                where = (f"AND (julianday(l.{column}) < julianday(:t) OR "
                         f"(julianday(l.{column}) = julianday(:t) AND l.link_id < :lid)) ")
        return list(self.conn.execute(
            "SELECT l.link_id, l.link_type, l.from_object, l.to_object, l.tier, "
            "l.binding_kind, l.reason, l.evidence_handles, l.recorded_at, l.superseded_at, "
            "l.markings FROM links l "
            "WHERE (l.from_object = :id OR l.to_object = :id) "
            f"AND l.{column} IS NOT NULL AND julianday(l.{column}) <= julianday(:at) "
            "AND l.markings IN (SELECT value FROM json_each(:marks)) " + where
            + f"ORDER BY julianday(l.{column}) DESC, l.link_id DESC LIMIT :limit", params))

    def marked_links(self, scope: _Scope, object_id: str) -> bool:
        """Whether a link with an end at ``object_id``, recorded by the scope's time,
        carries markings this reader does not hold (``link_history`` leaves it out)."""
        return self.conn.execute(
            "SELECT 1 FROM links l WHERE (l.from_object = :id OR l.to_object = :id) "
            "AND julianday(l.recorded_at) <= julianday(:at) "
            "AND l.markings NOT IN (SELECT value FROM json_each(:marks)) LIMIT 1",
            scope.params(id=object_id)).fetchone() is not None

    def objects(self, object_ids: Sequence[str]) -> dict[str, sqlite3.Row]:
        return {row["object_id"]: row for row in self.conn.execute(
            "SELECT * FROM objects WHERE object_id IN (SELECT value FROM json_each(?))",
            (json.dumps(list(object_ids)),))}

    def values(self, object_ids: Sequence[str]) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM object_values WHERE object_id IN (SELECT value FROM json_each(?)) "
            "ORDER BY object_id, property, authority_rank IS NULL, authority_rank, l1_handle, "
            "field_path", (json.dumps(list(object_ids)),)))

    def identifiers_of(self, scope: _Scope, object_ids: Sequence[str]) -> list[sqlite3.Row]:
        """Identifiers of readable objects current at :at with permitted markings, with
        the digest of the version they were read from."""
        return list(self.conn.execute(
            "SELECT i.*, o.object_type, o.binding_id, v.digest FROM identifiers i "
            "JOIN objects o ON o.object_id = i.object_id "
            "JOIN object_versions v ON v.object_id = i.object_id AND v.l1_handle = i.l1_handle "
            "WHERE i.object_id IN (SELECT value FROM json_each(:ids)) AND "
            + self._VISIBLE_OBJECT + "AND " + self._at("i") + " "
            "AND i.markings IN (SELECT value FROM json_each(:marks)) "
            "ORDER BY i.object_id, i.identifier_kind, i.l1_handle, i.field_path",
            scope.params(ids=json.dumps(list(object_ids)))))

    def links_touching(self, scope: _Scope, object_ids: Sequence[str],
                       link_types: Sequence[str]) -> list[sqlite3.Row]:
        """Links of ``link_types`` with an end among ``object_ids``, current at :at with
        permitted markings (ends and evidence are re-checked by the caller)."""
        return list(self.conn.execute(
            "SELECT * FROM links l WHERE l.link_type IN (SELECT value FROM json_each(:types)) "
            "AND (l.from_object IN (SELECT value FROM json_each(:ids)) "
            "OR l.to_object IN (SELECT value FROM json_each(:ids))) AND " + self._at("l")
            + " AND l.markings IN (SELECT value FROM json_each(:marks)) ORDER BY l.link_id",
            scope.params(ids=json.dumps(list(object_ids)),
                         types=json.dumps(list(link_types)))))

    def withheld_links(self, scope: _Scope, object_ids: Sequence[str],
                       link_types: Sequence[str]) -> list[sqlite3.Row]:
        """(from_object, to_object, tier) of the links of ``link_types`` with an end among
        ``object_ids``, current at :at, whose markings this reader does not hold: what
        ``links_touching`` leaves out, never to be read as no link."""
        return list(self.conn.execute(
            "SELECT l.from_object, l.to_object, l.tier FROM links l WHERE l.link_type IN "
            "(SELECT value FROM json_each(:types)) AND (l.from_object IN (SELECT value FROM "
            "json_each(:ids)) OR l.to_object IN (SELECT value FROM json_each(:ids))) AND "
            + self._at("l") + " AND l.markings NOT IN (SELECT value FROM json_each(:marks))",
            scope.params(ids=json.dumps(list(object_ids)),
                         types=json.dumps(list(link_types)))))

    def links_of(self, object_id: str) -> list[sqlite3.Row]:
        """Every link row with ``object_id`` at an end, read for no reader (the caller
        judges each row for its own); inside ``snapshot_reads`` read once per object (a
        margin's plan runs read the customer's thousands of links per order)."""
        snap = _SNAPSHOT.get()
        if snap is None:
            return self._links_of_read(object_id)
        memo = snap.setdefault("links", {})
        key = (id(self), object_id)
        if key not in memo:
            memo[key] = self._links_of_read(object_id)
        return list(memo[key])

    def _links_of_read(self, object_id: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM links WHERE from_object = ? OR to_object = ? ORDER BY link_id",
            (object_id, object_id)))

    def links_of_many(self, object_ids: Iterable[str]) -> dict[str, list[Any]]:
        """``links_of`` of each of ``object_ids`` (the same links, in the same order, each
        row holding ``HOP_COLUMNS`` at least), read in one statement rather than one per
        object (a record's children and their links: up to ``BEYOND_CHILDREN`` reads per
        hop); inside ``snapshot_reads`` each object's rows are kept (apart from
        ``links_of``'s full rows, which are reused when already read)."""
        ids = list(dict.fromkeys(object_ids))
        snap = _SNAPSHOT.get()
        full = snap.setdefault("links", {}) if snap is not None else {}
        memo = snap.setdefault("links_hop", {}) if snap is not None else None
        kept = {oid: full.get((id(self), oid)) for oid in ids}
        if memo is not None:
            kept = {oid: rows if rows is not None else memo.get((id(self), oid))
                    for oid, rows in kept.items()}
        wanted = [oid for oid in ids if kept[oid] is None]
        read: dict[str, list[Any]] = {oid: [] for oid in wanted}
        if wanted:
            for row in self.conn.execute(
                    "SELECT " + ", ".join(self.HOP_COLUMNS) + " FROM links "
                    "WHERE from_object IN (SELECT value FROM json_each(:ids)) "
                    "OR to_object IN (SELECT value FROM json_each(:ids)) ORDER BY link_id",
                    {"ids": json.dumps(wanted)}):
                for end in {row["from_object"], row["to_object"]}:
                    if end in read:   # a link between two of them is each one's
                        read[end].append(row)
            if memo is not None:
                for oid, rows in read.items():
                    memo[(id(self), oid)] = rows
        return {oid: list(read[oid] if kept[oid] is None else kept[oid]) for oid in ids}

    def linked_markings(self, root_id: str, link_type: str) -> dict[str, frozenset[str]]:
        """For each object ``link_type`` currently attaches to ``root_id`` (either end), the
        markings it carries: its link rows, every version and every current value (a marking
        added later applies to the earlier versions too). Read for no reader and answered to
        none: a caller uses it only to decide whether one reader's markings suffice
        (``read_scope``), never to show a row."""
        marks: dict[str, set[str]] = {}
        for link in self.conn.execute(
                "SELECT from_object, to_object, markings FROM links WHERE link_type = ? "
                "AND superseded_at IS NULL AND (from_object = ? OR to_object = ?)",
                (link_type, root_id, root_id)):
            other = link["to_object"] if link["from_object"] == root_id else link["from_object"]
            marks.setdefault(other, set()).update(json.loads(link["markings"]))
        if marks:
            ids = json.dumps(sorted(marks))
            for table, where in (("object_versions", ""),
                                 ("object_values", " AND superseded_at IS NULL")):
                for row in self.conn.execute(
                        f"SELECT object_id, markings FROM {table} WHERE object_id IN "
                        f"(SELECT value FROM json_each(?)){where}", (ids,)):
                    marks[row["object_id"]].update(json.loads(row["markings"]))
        return {key: frozenset(value) for key, value in marks.items()}
