"""``OntologyTools`` steps shared by every tool: the reader's scope, store facts,
freshness and gaps, row visibility, carriers and the link filters' schema checks."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import sqlite3
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from ontology.json_contract import validate
from ontology.types import Actor
from operating.ontology_tools_identity import (_gap, _predicate_text, _schema_memo,
                                               _STORE_KEPT_LOCK, _STORE_KEPT_MAX, _tested_marks,
                                               ToolError)
from operating.ontology_tools_policy import (_AUDIENCE_SOURCE, _bc_company_dropped,
                                             _format_time, _HEX64, _ITEM_ACL_AUDIENCES,
                                             _ITEM_ACL_SCHEMES, _known_at, _markings,
                                             _parse_time, _Scope, _to_ms, _type_keys,
                                             capture_failures, reader_grants, run_memo_for)
from operating.freshness_cadence import aged
from operating.ontology_tools_results import _source_freshness_gaps

if TYPE_CHECKING:
    from operating.ontology_tools_identity import _Key


class ScopeSteps:
    """Scope, freshness and visibility steps of ``OntologyTools``."""

    # ------------------------------------------------------------------ scope/meta

    def _as_known_at(self, arguments: Mapping[str, Any]) -> dt.datetime:
        text = arguments.get("as_known_at")
        if text is None:
            return self.clock().astimezone(dt.timezone.utc)
        moment = _parse_time(text, end_of_day=True)
        if moment is None:
            raise ToolError("invalid_input", "as_known_at is not a valid date or timestamp")
        return moment

    def _scope(self, actor: Actor, at: dt.datetime) -> _Scope:
        keys, held, sets, granted = reader_grants(self, self.policy, actor,
                                                  self.store.audience_keys())
        from operating.pg.people_excluded import filter_tag
        return _Scope(actor, keys, held, sets, granted, at, filter_tag(self, actor))

    def prefetch_items(self, actor: Actor, handles: Iterable[Any]) -> None:
        """Let the item gate read ahead, in batches, what it will read to decide each of
        ``handles`` (a gate offering ``prefetch``; any other reads nothing). Decides
        nothing: ``_item_allowed`` still decides every handle one by one, as before. A
        failing read-ahead is ignored (each decision then reads for itself)."""
        prefetch = getattr(self.item_gate, "prefetch", None)
        if prefetch is None:
            return
        kept = run_memo_for(("item_allowed", id(self), actor))
        wanted = [h for h in dict.fromkeys(handles) if isinstance(h, str)
                  and h.split("://", 1)[0] in _ITEM_ACL_SCHEMES
                  and (kept is None or h not in kept)]
        if not wanted:
            return
        try:
            prefetch(actor, wanted)
        except Exception:  # noqa: BLE001 - a read-ahead never decides; the gate reads alone
            pass

    def _item_allowed(self, actor: Actor, handle: str) -> bool:
        scheme = handle.split("://", 1)[0] if isinstance(handle, str) else ""
        if scheme not in _ITEM_ACL_SCHEMES:
            return True
        if self.item_gate is None:
            return False
        kept = run_memo_for(("item_allowed", id(self), actor))
        if kept is not None and handle in kept:
            return kept[handle]
        try:
            allowed = self.item_gate(actor, handle) is True
        except Exception:  # noqa: BLE001 - a failing re-check denies
            allowed = False
        if kept is not None:
            kept[handle] = allowed
        return allowed

    def _store_meta(self) -> dict[str, str]:
        try:
            return self.store.meta()
        except sqlite3.Error:
            return {}

    def _dropped_version_gaps(self, object_ids: Iterable[str],
                              store_meta: Mapping[str, str] | None = None) -> list[dict]:
        """A gap per object whose binding lost BC versions at build time because
        their company was unknown (``store_meta.build_report`` code
        ``bc_company_unknown``), so a reader is never shown fewer versions silently.
        The report counts per binding, so the gap carries no count and does
        not claim which versions are this object's."""
        text = (store_meta if store_meta is not None
                else self._store_meta()).get("build_report", "")
        dropped = _bc_company_dropped(text)
        if dropped is None:
            return []
        out = []
        for object_id in object_ids:
            parts = object_id.split(":", 2)
            if len(parts) == 3 and dropped.get(parts[1]):
                out.append(_gap("unknown", "versions_dropped_bc_company_unknown", object_id,
                                f"some versions of this object's binding ({parts[1]}) could "
                                "not be projected because their BC company is unknown; "
                                "this object may have more versions than shown"))
        return out

    def _unparsed_value_gaps(self, scope: _Scope, object_id: str, handle: str,
                             type_spec: Mapping[str, Any], store_meta: Mapping[str, str],
                             wanted: set[str] | None, read: set[str]) -> dict[str, dict]:
        """property -> gap, for each property of the object with no stored read (not in
        ``read``) whose source on the object's binding the projector reported leaving a
        value out at (``store_meta.build_report``, ``Type.property@binding``). The projector
        drops a value only when the record fails the source's ``when`` tests
        (``source_condition_unmet``: the field does not mean the property on this record),
        its path is absent (``path_absent``) or it fails to parse (``parse_``). The build
        records every version failing the tests (``store_meta.source_condition_unmet``):
        to a reader holding the markings of the fields the tests read (``_tested_marks``)
        the gap is ``source_condition_unmet`` when it names the object's version
        (``handle``), and the tests are no cause when it does not. To any other reader (or
        on a store without that record) it is ``source_condition_unmet`` only when that is
        the one code reported there, else one possible cause. Then: none when an absent
        path's samples name the version; ``value_unparsed`` when a parse failure's samples
        name the version or it is the only code reported there; ``value_unread`` when a
        parse failure or the unmet tests and another code may each be the cause; and none
        when only an absent path was reported. A property whose markings the reader lacks
        gets none: a parsed value would have been withheld from them."""
        object_type, _, rest = object_id.partition(":")
        binding = rest.partition(":")[0]
        try:
            report = json.loads(store_meta.get("build_report", ""))
        except ValueError:
            return {}
        at: dict[str, list[Mapping[str, Any]]] = {}
        for gap in report.get("gaps", ()) if isinstance(report, dict) else ():
            if isinstance(gap, dict) and isinstance(gap.get("code"), str) \
                    and isinstance(gap.get("where"), str):
                at.setdefault(gap["where"], []).append(gap)

        def sampled(gaps: Sequence[Mapping[str, Any]]) -> bool:
            return any(handle in (g.get("samples") if isinstance(g.get("samples"), list)
                                  else ()) for g in gaps)

        recorded: list[Any] = []

        def unmet_at(where: str) -> bool | None:
            """Whether the build recorded this version failing the tests at ``where``;
            None when the store holds no such record of ``where`` (never a pass)."""
            if not recorded:
                try:
                    loaded = json.loads(store_meta.get("source_condition_unmet", ""))
                except ValueError:
                    loaded = None
                recorded.append(loaded if isinstance(loaded, dict) else None)
            if recorded[0] is None:
                return None
            handles = recorded[0].get(where)
            return handle in handles if isinstance(handles, list) else None

        out: dict[str, dict] = {}
        for prop in type_spec.get("properties", ()):
            name = prop.get("name") if isinstance(prop, Mapping) else None
            if not isinstance(name, str) or name in read or (
                    wanted is not None and name not in wanted) \
                    or not scope.holds(prop.get("markings", ())):
                continue
            here = at.get(f"{object_type}.{name}@{binding}", ())
            unmet = [g for g in here if g["code"] == "source_condition_unmet"]
            failed = [g for g in here if g["code"].startswith("parse_")]
            absent = [g for g in here if g["code"] == "path_absent"]
            subject = f"{object_type}.{name}"
            marks = _tested_marks(prop, binding, type_spec) if unmet else None
            fails = (unmet_at(f"{object_type}.{name}@{binding}")
                     if marks is not None and scope.holds(marks) else None)
            if fails is False:
                unmet = []   # recorded as passing the tests: they did not drop its value
            if fails or (unmet and not failed and not absent):
                tests = [src.get("when") for src in prop.get("sources", ())
                         if isinstance(src, Mapping) and src.get("binding") == binding]
                stated = "; ".join(" and ".join(_predicate_text(w) for w in when)
                                   for when in tests if isinstance(when, list) and when)
                out[name] = _gap("unknown", "source_condition_unmet", subject,
                                 "this record's source states the property only on records "
                                 f"that pass its tests{f' ({stated})' if stated else ''}; "
                                 "this record does not, so it states no value of it; absence "
                                 "elsewhere is not established")
                continue
            if not (failed or unmet) or sampled(absent):
                continue
            codes = ", ".join(sorted({g["code"][len("parse_"):] for g in failed}))
            found = ("; a find by this value does not reach this record"
                     if prop.get("identifier_kind") else "")
            if failed and (sampled(failed) or not (absent or unmet)):
                out[name] = _gap("unknown", "value_unparsed", subject,
                                 f"the projection could not parse this record's value "
                                 f"({codes}), so it is left out{found}; absence from the "
                                 "source is not established")
            else:
                causes = [c for c, on in (("the field was absent", absent),
                                          (f"its value could not be parsed ({codes})", failed),
                                          ("the record failed the source's tests", unmet))
                          if on]
                out[name] = _gap("unknown", "value_unread", subject,
                                 "the projection left this record's value out: "
                                 f"{', '.join(causes[:-1])} or {causes[-1]}{found}; absence "
                                 "from the source is not established")
        return out

    def _unparsed_carriers(self, scope: _Scope, object_type: str | None,
                           store_meta: Mapping[str, str]) -> list[str]:
        """``Type.property`` of each identifier property some values of which the projector
        could not parse (a ``parse_`` failure at ``Type.property@binding`` in
        ``store_meta.build_report``): a record holding one is not found by that value. Only
        ``object_type``'s when given, and only those this reader could read: it holds the
        type's and the property's markings and may read some object of that binding."""
        if self.schema is None:
            return []
        try:
            report = json.loads(store_meta.get("build_report", ""))
        except ValueError:
            return []
        failed: dict[str, set[str]] = {}
        for gap in report.get("gaps", ()) if isinstance(report, dict) else ():
            if isinstance(gap, dict) and str(gap.get("code", "")).startswith("parse_"):
                name, _, binding = str(gap.get("where", "")).partition("@")
                if binding:
                    failed.setdefault(name, set()).add(binding)
        wanted: list[tuple[str, str, str]] = []   # (Type.property, type, binding)
        for name, bindings in sorted(failed.items()):
            tid, _, prop_name = name.partition(".")
            if object_type is not None and tid != object_type:
                continue
            spec = self.schema.object_types.get(tid) or {}
            prop = next((p for p in spec.get("properties", ()) if isinstance(p, Mapping)
                         and p.get("name") == prop_name), None)
            if prop is None or not prop.get("identifier_kind") or not scope.holds(
                    set(spec.get("markings", ())) | set(prop.get("markings", ()))):
                continue
            wanted += [(name, tid, binding) for binding in sorted(bindings)]
        if not wanted:
            return []
        readable = self._bindings_readable(scope, [(tid, binding)
                                                   for _name, tid, binding in wanted])
        return sorted({name for name, tid, binding in wanted if (tid, binding) in readable})

    def _bindings_readable(self, scope: _Scope,
                           pairs: Sequence[tuple[str, str]]) -> set[tuple[str, str]]:
        """Each ``(type, binding)`` of ``pairs`` that ``_binding_readable`` holds, with
        the same kept facts and kept answers; the answers not kept yet are probed in one
        statement (a statement per binding was a round trip each on Postgres)."""
        audiences = frozenset(scope.audience_keys)
        typed = self._store_fact("audiences")
        candidates = [(tid, binding) for tid, binding in dict.fromkeys(pairs)
                      if not isinstance(typed, Mapping)
                      or audiences.intersection(typed.get(tid, ()))]
        if not candidates:
            return set()
        stamp = self._store_stamp()
        with _STORE_KEPT_LOCK:
            kept = getattr(self.store, "_kept_answers", None)
            held = set(kept[1]) if stamp is not None and kept is not None \
                and kept[0] == stamp else set()
        misses = [pair for pair in candidates
                  if ("binding_readable", *pair, audiences) not in held]
        found: set[tuple[str, str]] = set()
        params = scope.params()
        for start in range(0, len(misses), 400):   # under SQLite's 500 compound terms
            probes = []
            for at, (tid, binding) in enumerate(misses[start:start + 400], start):
                params[f"tid{at}"], params[f"bid{at}"] = tid, binding
                params[f"aud{at}"] = _type_keys(typed, tid, audiences, params["aud"])
                probes.append(f"SELECT {at} AS at WHERE EXISTS (SELECT 1 FROM objects WHERE "
                              f"object_type = :tid{at} AND binding_id = :bid{at} AND "
                              f"audience_key IN (SELECT value FROM json_each(:aud{at})))")
            found.update(misses[int(row[0])] for row in self.store.conn.execute(
                " UNION ALL ".join(probes), params).fetchall())
        return {pair for pair in candidates
                if self._store_kept(("binding_readable", *pair, audiences),
                                    lambda pair=pair: pair in found)}

    def _binding_readable(self, scope: _Scope, tid: str, binding: str) -> bool:
        """Whether some object of ``tid`` from ``binding`` carries an audience key of
        ``scope``. No index leads with an audience key, so a reader holding none of a
        large binding's keys scanned all of it (3.3M mail messages: 20 to 40 s a find,
        live 2026-11-07). The store's kept facts (``TraverseTools._audiences``: each
        type's audience keys, read for every reader at once) answer no without a scan
        when the reader holds none of the type's keys; every answer is kept on the
        store's reader per store state and audience list (``_store_kept``)."""
        audiences = frozenset(scope.audience_keys)
        typed = self._store_fact("audiences")
        if isinstance(typed, Mapping) and not audiences.intersection(typed.get(tid, ())):
            return False
        params = scope.params(tid=tid, bid=binding)
        params["aud"] = _type_keys(typed, tid, audiences, params["aud"])
        return self._store_kept(("binding_readable", tid, binding, audiences), lambda: (
            self.store.conn.execute(
                "SELECT 1 FROM objects WHERE object_type = :tid AND binding_id = :bid "
                "AND audience_key IN (SELECT value FROM json_each(:aud)) LIMIT 1",
                params).fetchone() is not None))

    def _store_stamp(self) -> tuple[int, int] | None:
        """The store state the kept facts and answers are for, as ``TraverseTools``
        stamps them: a SQLite file's data version, or on Postgres the loaded layers'
        (``PRAGMA data_version`` through the compatibility connection). None (nothing
        kept) for any other store. Postgres once read as None here, so its kept facts
        were never used and every untyped find probed every unparsed binding again
        (a 51 s mail probe in each cold call)."""
        conn = getattr(self.store, "conn", None)
        if not isinstance(conn, sqlite3.Connection) and not getattr(conn, "postgres", False):
            return None
        version = getattr(self.store, "data_version", None)
        return (version() if version is not None
                else conn.execute("PRAGMA data_version").fetchone()[0], conn.total_changes)

    def _store_fact(self, key: str) -> Any:
        """A reader-independent fact the store's reader keeps for its current state
        (``TraverseTools._store_cached``), or None when it holds none for that state."""
        facts = getattr(self.store, "facts", None)
        stamp = self._store_stamp()
        if stamp is None or not isinstance(facts, tuple) or facts[0] != stamp:
            return None
        return facts[1].get(key)

    def _store_kept(self, key: Any, read: Callable[[], Any]) -> Any:
        """``read()`` kept on the store's reader while the store's state is unchanged (a
        commit on another connection or a write on this one reads it again); a store
        without a SQLite connection reads it every time."""
        stamp = self._store_stamp()
        if stamp is None:
            return read()
        with _STORE_KEPT_LOCK:
            kept = getattr(self.store, "_kept_answers", None)
            if kept is None or kept[0] != stamp:
                kept = (stamp, {})
                try:
                    self.store._kept_answers = kept
                except AttributeError:
                    return read()
            if key in kept[1]:
                return kept[1][key]
        value = read()
        with _STORE_KEPT_LOCK:
            if len(kept[1]) >= _STORE_KEPT_MAX:
                kept[1].clear()
            kept[1][key] = value
        return value

    def _schema_digest(self, store_meta: Mapping[str, str]) -> str | None:
        found = _HEX64.match(store_meta.get("schema_digest", ""))
        if found:
            return found.group(1)
        return self.schema.digest_hex if self.schema else None

    def _freshness(self, store_meta: Mapping[str, str], sources: Iterable[str]) -> list[dict]:
        """Per-source freshness for the sources an answer used. store_meta
        ``freshness`` is a list of contract freshness objects or a mapping
        source -> the same object without ``source``; malformed entries are
        ignored and the source then reads as captured_at null, completeness
        unknown (with a gap), never as fresh."""
        try:
            raw = json.loads(store_meta.get("freshness", "[]"))
        except ValueError:
            raw = []
        if isinstance(raw, dict):
            items = [dict(v, source=k) for k, v in raw.items() if isinstance(v, dict)]
        else:
            items = raw if isinstance(raw, list) else []
        entries: dict[str, dict] = {}
        for item in items:
            if isinstance(item, dict) and not validate(
                    item, self.contracts["$defs"]["freshness"], root=self.contracts, limit=1):
                entries[item["source"]] = item
        out = []
        for source in sorted(set(sources))[:8]:
            out.append(self._aged(entries.get(source) or {
                "source": source, "captured_at": None, "completeness": "unknown",
                "stale": False}))
        return out

    def _aged(self, entry: dict) -> dict:
        """A projected source's entry judged by this host's capture bound for it: past
        the bound it is stale, labelled with its captured time and age (one
        ``stale_source`` gap, ``_source_freshness_gaps``). An entry already stale (a
        copy served while the live source was out) keeps its own label."""
        return aged(entry, self.freshness_bounds, (self.freshness_clock or self.clock)(),
                    self._capture_failures(self._served_at()))

    def _served_at(self) -> str | None:
        """The served generation's build time (``store_meta.built_at``), or None."""
        try:
            row = self.store.conn.execute(
                "SELECT value FROM store_meta WHERE key = 'built_at'").fetchone()
        except (sqlite3.Error, AttributeError):
            return None
        return row[0] if row else None

    def _capture_failures(self, served_at: Any = None) -> frozenset[str]:
        """``capture_failures`` of the host ledger beside this store's state directory
        (``<state>/projection/<store>`` -> ``<state>/host/host.sqlite``); none without."""
        try:
            rows = self.store.conn.execute("PRAGMA database_list").fetchall()
        except (sqlite3.Error, AttributeError):
            return frozenset()
        main = next((row[2] for row in rows if row[1] == "main" and row[2]), None)
        if not main:
            return frozenset()
        return capture_failures(Path(main).parent.parent / "host" / "host.sqlite",
                                served_at)

    def _meta(self, store_meta: Mapping[str, str], at: dt.datetime,
              sources: Iterable[str]) -> dict[str, Any]:
        digest = self._schema_digest(store_meta)
        if digest is None:
            raise ToolError("schema_unavailable",
                            "neither the projection store nor the schema loader names a "
                            "schema digest", "build the projection (lane L7) or load the schema")
        return {"schema_digest": digest, "as_known_at": _format_time(at),
                "freshness": self._freshness(store_meta, sources)}

    def _source_gaps(self, freshness: Sequence[Mapping[str, Any]],
                     elsewhere: Mapping[str, Sequence[str]] | None = None) -> list[dict]:
        """``_source_freshness_gaps``, except that a source this host configured no
        reader for, and that nothing was read from (no capture time, no unit served),
        has no unknown or unavailable state to report: it states once that it is not
        configured. Anything read from it keeps its freshness gaps."""
        unread = {item["source"] for item in freshness if item["source"] in self.unconfigured
                  and not item.get("captured_at")
                  and item.get("served_from") in (None, "none")
                  and all(u.get("served_from") == "none" for u in item.get("relations", ()))}
        gaps, dropped = [], set()
        for gap in _source_freshness_gaps(freshness, elsewhere):
            if gap["subject"] in unread and gap["reason"] in (
                    "freshness_unknown", "source_unavailable"):
                dropped.add(gap["subject"])
            else:
                gaps.append(gap)
        return gaps + [_gap("not_ingested", "source_not_configured", source,
                            "this host configures no reader for this source, so it was not "
                            "read: nothing is known about what it holds")
                       for source in sorted(dropped)]

    def _freshness_gaps(self, meta: Mapping[str, Any], store_meta: Mapping[str, str],
                        types: Iterable[str] = (), link_types: Iterable[str] = ()
                        ) -> list[dict]:
        """Freshness gaps of the sources ``meta`` lists. Given the object ``types`` an
        answer's records are of and the ``link_types`` it walked, a source read only in
        part whose unread relations hold none of them (``partly_read_elsewhere``) says so
        instead of ``source_partly_read``: the part not read could hold no record of this
        answer, so a count over it stays exact."""
        elsewhere = self._partly_read_elsewhere(store_meta, types, link_types) \
            if types else {}
        gaps = self._source_gaps(meta["freshness"], elsewhere)
        store_digest = _HEX64.match(store_meta.get("schema_digest", ""))
        if store_digest and self.schema and self.schema.digest_hex \
                and store_digest.group(1) != self.schema.digest_hex:
            gaps.append(_gap("stale", "schema_drift", "projection",
                             "the projection was built from another schema version"))
        return gaps

    def _partly_read_elsewhere(self, store_meta: Mapping[str, str], types: Iterable[str],
                               link_types: Iterable[str] = ()) -> dict[str, list[str]]:
        """Source family -> its relations read only in part (or served from a copy), for
        each family whose part not read cannot hold a record of ``types`` reached over
        ``link_types``: every relation a binding of those types, or of those links (a
        ``via`` table), reads in that family is complete and not stale in
        ``store_meta.relation_completeness``. Fail closed: a link without a stored-key
        binding (a mention, a text match), an undeclared type or link, a family with
        no relation of those types, or a relation the store states no completeness for
        leaves the family out (its ``source_partly_read`` gap stands)."""
        schema = self.schema
        if schema is None:
            return {}
        try:
            raw = json.loads(store_meta.get("relation_completeness", "{}"))
        except (TypeError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        needed: dict[str, set[str]] = {}
        by_id: dict[str, Mapping[str, Any]] = {}
        for name in schema.object_types:
            for binding in schema.object_types[name].get("bindings", ()):
                if isinstance(binding.get("source"), str) \
                        and isinstance(binding.get("relation"), str):
                    by_id[binding.get("id")] = binding
        for name in set(types):
            spec = schema.object_types.get(name)
            if spec is None:
                return {}
            for binding in spec.get("bindings", ()):
                if binding.get("id") not in by_id:
                    return {}   # a binding naming no source relation: not decidable
                needed.setdefault(binding["source"], set()).add(binding["relation"])
        for name in set(link_types):
            link = schema.link_types.get(name)
            if link is None:
                return {}
            for binding in link.get("bindings", ()):
                ids = [binding.get("from_binding"), binding.get("to_binding")]
                via = binding.get("via")
                if isinstance(via, Mapping):
                    ids.append(via.get("binding"))
                ids = [i for i in ids if i is not None]
                if binding.get("kind") not in ("fk", "same_record", "json_path") \
                        or not ids or any(i not in by_id for i in ids):
                    return {}
                for i in ids:
                    needed.setdefault(by_id[i]["source"], set()).add(by_id[i]["relation"])
        out: dict[str, list[str]] = {}
        for source, relations in raw.items():
            facts = relations if isinstance(relations, dict) else {}
            unread = sorted(r for r, f in facts.items() if not (
                isinstance(f, dict) and f.get("completeness") == "complete"
                and f.get("stale") is False))
            mine = needed.get(source)
            if not unread or not mine or any(r not in facts for r in mine) \
                    or set(unread) & mine:
                continue
            out[source] = unread
        return out

    # ------------------------------------------------------------------ visibility

    def _visible_versions(self, scope: _Scope, object_ids: Iterable[str]
                          ) -> dict[str, sqlite3.Row]:
        """object_id -> version current at T, for each object the reader may read:
        audience, version and latest-version markings and time are decided in SQL,
        then the item ACL is re-checked through the version's L1 handle."""
        ids = sorted(set(object_ids))
        out: dict[str, sqlite3.Row] = {}
        for start in range(0, len(ids), 500):
            out.update(self._gated_versions(
                scope, self.store.visible_versions(scope, ids[start:start + 500])))
        return out

    def _gated_versions(self, scope: _Scope, versions: Sequence[sqlite3.Row]
                        ) -> dict[str, sqlite3.Row]:
        """object_id -> version of those ``versions`` (``visible_versions``'s rows, the
        SQL rules already applied) whose item ACL the reader passes."""
        out: dict[str, sqlite3.Row] = {}
        # what the item gate reads of each handle, read ahead in batches (decides
        # nothing: every version below is still gated one by one)
        self.prefetch_items(scope.actor, [version["l1_handle"] for version in versions])
        for version in versions:
            if self._item_allowed(scope.actor, version["l1_handle"]):
                out[version["object_id"]] = version
        return out

    def _row_visible(self, scope: _Scope, row: Mapping[str, Any]) -> bool:
        return bool(_known_at(row, scope.at)) and scope.holds(_markings(row["markings"]))

    def _absence_proven(self, scope: _Scope, arguments: Mapping[str, Any],
                        freshness: Sequence[Mapping[str, Any]], truncated: bool,
                        declared: Iterable[str]) -> bool:
        """A no-match is a business fact (absent_in_source) only when the reader's
        grant covers every audience unit of every searched source, it holds every
        marking the schema declares on what was searched (``declared``,
        ``_declared_markings``), no searched source has item-level ACLs, the search
        was not truncated, the answer is about now and every source's capture is
        complete. Decided from the policy, the schema and the call, never from rows
        the reader cannot see."""
        if truncated or not scope.sources or not freshness or not scope.holds(declared):
            return False
        if "as_known_at" in arguments and scope.at < _to_ms(self.clock()):
            return False
        prefixes = {source: prefix for prefix, source in _AUDIENCE_SOURCE.items()}
        for source in scope.sources:
            prefix = prefixes[source]
            if prefix in _ITEM_ACL_AUDIENCES or not self.policy.covers_family(scope.actor, prefix):
                return False
        return all(f.get("completeness") == "complete" and not f.get("stale") for f in freshness)

    def _declared_markings(self, object_type: str | None, carried: Sequence[str],
                           filters: Sequence[Mapping[str, Any]]) -> frozenset[str]:
        """Every marking the schema declares on what a find searched: the asked type
        (else the types of the identifier carriers searched), those carriers and the
        filtered properties. A record of a marked type, or a marked value, is projected
        with these markings (``ontology/project.py``), so a reader lacking one may not
        see every record the search covers."""
        types = self.schema.object_types if self.schema is not None else {}
        wanted = {object_type} if object_type is not None else {
            name.split(".", 1)[0] for name in carried}
        named = {tuple(name.split(".", 1)) for name in carried}
        named |= {(tid, f["property"]) for tid in wanted for f in filters}
        marks: set[str] = set()
        for tid in wanted | {tid for tid, _ in named}:
            marks |= set((types.get(tid) or {}).get("markings", ()))
        for tid, prop in named:
            for spec in (types.get(tid) or {}).get("properties", ()):
                if isinstance(spec, Mapping) and spec.get("name") == prop:
                    marks |= set(spec.get("markings", ()))
        return frozenset(marks)

    def _reviewed_aliases(self, scope: _Scope, keys: Sequence[_Key]) -> list[dict[str, Any]]:
        """Index rows of the reviewed aliases of ``keys`` this reader may use."""
        return self.aliases.rows(self.store.conn, scope.actor.tenant,
                                 [(k.kind, k.normalized) for k in keys if k.kind],
                                 scope.params(),
                                 lambda handle: self._item_allowed(scope.actor, handle))

    def _carriers_searched(self, query: str | None, keys: Sequence[_Key],
                           object_type: str | None, store_meta: Mapping[str, str],
                           sources: Sequence[str]) -> tuple[list[str], list[str]]:
        """(carriers searched, carriers missed) of a key search: every identifier
        property (``<Type>.<property>``, renumbered keys included; a declared reference
        names another record, so it is none) of the asked type, or without one of every
        type with a carrier of a kind the query reads as (searched or not). A carrier is
        searched when a key of its kind was tried, every binding
        it is sourced from is in a source the reader searched (``sources``) and it and
        its type are served (not ``degraded`` or ``not_served``). It is missed when the
        query reads as its kind but that kind was not searched (narrowed away by
        ``identifier_kind``), when a binding of it lies outside the reader's sources,
        when it or its type is not fully served, or when the build could not parse one
        of its values (``parse_*`` in the build report): a row may hold the key there
        unindexed. A
        carrier whose kind the query does not read as is neither (it cannot hold the
        key, and nothing was searched there). No schema, grammar or type with a
        carrier: nothing searched."""
        if self.schema is None or self.grammar is None or not query:
            return [], []
        tried = {k.kind for k in keys if k.whole and not k.secondary and k.kind}
        types = self.schema.object_types
        readings: dict[str, bool] = {}

        def reads(kind: str) -> bool:
            if kind not in readings:
                try:
                    readings[kind] = kind in tried or bool(self.grammar.normalize(kind, query))
                except Exception:  # noqa: BLE001 - a refused form is no reading
                    readings[kind] = False
            return readings[kind]

        held_carriers = _schema_memo(self.schema, "carriers", lambda: {
            tid: [p for p in (declared or {}).get("properties", ())
                  if isinstance(p, Mapping) and p.get("identifier_kind")
                  and p.get("identifies") != "reference"]
            for tid, declared in types.items()})

        def carriers(tid: str) -> list[Mapping[str, Any]]:
            return held_carriers.get(tid, [])

        wanted = [object_type] if object_type is not None else sorted(
            tid for tid in types if any(reads(p["identifier_kind"]) for p in carriers(tid)))
        try:
            report = json.loads(store_meta.get("build_report", ""))
        except ValueError:
            report = {}
        unparsed = {str(g.get("where", "")).partition("@")[0]
                    for g in (report.get("gaps", ()) if isinstance(report, dict) else ())
                    if isinstance(g, dict) and str(g.get("code", "")).startswith("parse_")}
        searched: list[str] = []
        missed: list[str] = []
        for tid in wanted:
            for prop in carriers(tid):
                name, kind = f"{tid}.{prop['name']}", prop["identifier_kind"]
                if kind not in tried:
                    if reads(kind):
                        missed.append(name)
                    continue
                (missed if name in unparsed or not self._carrier_reached(tid, prop, sources)
                 else searched).append(name)
        return searched, missed

    def _carrier_reached(self, tid: str, prop: Mapping[str, Any],
                         sources: Sequence[str]) -> bool:
        """Whether the property ``prop`` of type ``tid`` was read in full: its type and it
        are served (not ``degraded`` or ``not_served``) and every binding it is sourced
        from is in a source the reader searched (``sources``)."""
        spec = (self.schema.object_types if self.schema else {}).get(tid) or {}
        if (spec.get("availability") or {}).get("state", "served") != "served" or \
                prop.get("availability", "served") != "served":
            return False
        families = {b.get("id"): b.get("source") for b in spec.get("bindings", ())
                    if isinstance(b, Mapping)}
        used = {families.get(item.get("binding")) for item in prop.get("sources", ())
                if isinstance(item, Mapping)}
        return bool(used) and used <= set(sources)

    def _filters_as_stored(self, object_type: str, filters: Sequence[Mapping[str, Any]]
                           ) -> list[dict[str, Any]]:
        """``filters`` with each text value asked of an identifier property written as its
        kind's grammar stores it (a lower-case lot asks for the upper-cased one); a value
        the grammar does not read stays as asked."""
        props = {p["name"]: p for p in self.schema.object_types[object_type]
                 .get("properties", ()) if isinstance(p, Mapping)}

        def stored(kind: str, value: Any) -> Any:
            try:
                read = (self.grammar.normalize(kind, value)
                        if self.grammar is not None and isinstance(value, str) else None)
            except (KeyError, ValueError):
                read = None
            return read if isinstance(read, str) and read else value

        out = []
        for f in filters:
            prop = props.get(f["property"]) or {}
            kind = prop.get("identifier_kind") if prop.get("value_type") == "identifier" else None
            if kind is None or "value" not in f:
                out.append(dict(f))
                continue
            wanted = f["value"]
            out.append({**f, "value": [stored(kind, w) for w in wanted]
                        if isinstance(wanted, list) else stored(kind, wanted)})
        return out

    def link_names(self, object_type: str) -> dict[str, tuple[str, bool, str]]:
        """navigation name -> (link type, forward, type reached) of each declared link of
        ``object_type`` a filter may name (``ordered_from``, ``purchase_orders``); never a
        mention or identity link."""
        out: dict[str, tuple[str, bool, str]] = {}
        for link_id, spec in sorted(((self.schema.link_types or {}) if self.schema else {})
                                    .items()):
            if ".mentions_" in link_id or link_id.endswith(".same_record_as"):
                continue
            if spec.get("from") == object_type and spec.get("name"):
                out.setdefault(spec["name"], (link_id, True, spec["to"]))
            if spec.get("to") == object_type and spec.get("reverse_name"):
                out.setdefault(spec["reverse_name"], (link_id, False, spec["from"]))
        return out

    def _declared_names(self, object_type: str) -> frozenset[str]:
        spec = (self.schema.object_types if self.schema else {}).get(object_type) or {}
        return frozenset(p["name"] for p in spec.get("properties", ())
                         if isinstance(p, Mapping) and p.get("name"))

    def event_names(self, object_type: str) -> dict[str, str]:
        """name -> event type of each declared event of ``object_type`` (``entered`` for
        ``AssemblyOrderDamage.entered``): a filter may name it as a date the record holds
        only as its event."""
        events = (getattr(self.schema, "event_types", None) or {}) if self.schema else {}
        return {event_id.split(".", 1)[1]: event_id for event_id, spec in sorted(events.items())
                if spec.get("object_type") == object_type and "." in event_id}

    def _link_filters(self, object_type: str, filters: Sequence[Mapping[str, Any]]
                      ) -> list[Mapping[str, Any]]:
        """The filters naming a link of ``object_type`` (``link_names``) instead of a
        declared property: a record passes when that link reaches one of the records the
        filter names (``_linked_ids``)."""
        if self.schema is None or object_type not in self.schema.object_types:
            return []
        declared = {p["name"] for p in self.schema.object_types[object_type]
                    .get("properties", ()) if isinstance(p, Mapping)}
        names = self.link_names(object_type)
        return [f for f in filters if f.get("property") not in declared
                and f.get("property") in names]

    def unsourced_filter_gaps(self, object_type: str, filters: Sequence[Mapping[str, Any]],
                              kept: Callable[[str], bool] | None = None,
                              members: Iterable[str] | None = None
                              ) -> list[dict[str, Any]]:
        """A gap per filtered property some sources of the type never record: their
        records never match it (a supplier name only Business Central's purchase orders
        carry leaves the other systems' orders out), naming the links that reach the same
        related records in those sources, so the filter can name them instead. ``kept``:
        only the bindings it keeps are asked about (a count of one system's records leaves
        no other system's record out). ``members``: the records the filter was applied to
        (a walk's or a set's, before it): only the bindings they come from are asked about,
        since a source none of them comes from loses none of them (11-13: every filter on a
        walk's Sage orders said the BC and FileMaker orders were left out, so a count of
        them was never exact); a member of no declared binding of the type asks them all."""
        if self.schema is None or object_type not in self.schema.object_types:
            return []
        spec = self.schema.object_types[object_type]
        declared = {str(b.get("id")) for b in spec.get("bindings", ()) if isinstance(b, Mapping)}
        if members is not None:
            members = [str(oid) for oid in members]   # read twice below
            present = {str(oid).split(":", 2)[1] if str(oid).count(":") >= 2 else ""
                       for oid in members if str(oid).split(":", 1)[0] == object_type}
            other = any(str(oid).split(":", 1)[0] != object_type for oid in members)
            if not other and present <= declared:
                outer = kept
                kept = lambda binding: binding in present and (  # noqa: E731
                    outer is None or outer(binding))
        bindings = [b.get("id") for b in spec.get("bindings", ()) if isinstance(b, Mapping)
                    and (kept is None or kept(str(b.get("id"))))]
        props = {p["name"]: p for p in spec.get("properties", ()) if isinstance(p, Mapping)}
        out, seen = [], set()
        for f in filters:
            sources = (props.get(f.get("property")) or {}).get("sources")
            if f.get("op") in ("absent", "ne") or not sources or f["property"] in seen:
                continue
            seen.add(f["property"])
            held = {x.get("binding") for x in sources if isinstance(x, Mapping)}
            left = [b for b in bindings if b and b not in held]
            if not left:
                continue
            routes = sorted(name for name, (link_id, forward, _t)
                            in self.link_names(object_type).items()
                            if any((b.get("from_binding") if forward else b.get("to_binding"))
                                   in left for b in self.schema.link_types[link_id]
                                   .get("bindings", ()) if isinstance(b, Mapping)))
            out.append({"state": "unknown", "reason": "filter_not_sourced",
                        "subject": f"{object_type}.{f['property']}",
                        "detail": (f"{f['property']} is recorded only by "
                                   f"{', '.join(sorted(held))}: {object_type} records of "
                                   f"{', '.join(left)} never match this filter"
                                   + (f"; a filter on a link reaches the related records in "
                                      f"every source: {', '.join(routes)} (its records, a "
                                      "set, or a name prefix)" if routes else ""))[:600]})
        return out

    def _require_co_sourced(self, object_type: str, filters: Sequence[Mapping[str, Any]]
                            ) -> None:
        """Refuse filters no record can pass together. Each record of a type is one
        binding's row and holds only the properties that binding sources; every op but
        ``absent`` needs a value, so filters on properties no one binding sources both
        (a BC receipt's number and a Portal receipt's received date) match nothing, and
        their answer would read as a true zero. Refused with the bindings each comes
        from (``filters_never_co_sourced``), never answered as none."""
        if self.schema is None:
            return
        props = {p["name"]: p for p in self.schema.object_types[object_type]
                 .get("properties", ()) if isinstance(p, Mapping)}
        held: dict[str, set[str]] = {}
        for f in filters:
            sources = (props.get(f["property"]) or {}).get("sources")
            if f.get("op") == "absent" or not sources:
                continue
            held[f["property"]] = {s.get("binding") for s in sources
                                   if isinstance(s, Mapping)}
        common: set[str] | None = None
        for name, bindings in held.items():
            common = set(bindings) if common is None else common & bindings
            if not common:
                said = "; ".join(f"{n} from {', '.join(sorted(b))}" for n, b in held.items())
                raise ToolError(
                    "filters_never_co_sourced",
                    f"no {object_type} record can pass these filters together: no source "
                    f"records all of {', '.join(held)} ({said})"[:600],
                    "filter on properties one source records together; a date another "
                    "system records is its event (plx.schema event_types, then plx.timeline "
                    "of the records)")

    def _filters_carried(self, object_type: str, filters: Sequence[Mapping[str, Any]],
                         sources: Sequence[str]) -> bool:
        """Whether every property a filter-only find tested was read in full (see
        ``_carrier_reached``): else its no-match proves nothing about the source."""
        spec = (self.schema.object_types if self.schema else {}).get(object_type) or {}
        props = {p.get("name"): p for p in spec.get("properties", ()) if isinstance(p, Mapping)}
        return all(f.get("property") in props
                   and self._carrier_reached(object_type, props[f["property"]], sources)
                   for f in filters)
