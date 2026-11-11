"""The step migration layer, checkpointed: a stopped build resumes after its last
finished step instead of from the start.

Measured on the live host (the lp2 step layer, 2026-11-07/08): the input copy of the
served ontology took about an hour and 89-105 GB, the mentions step 3.5-4 h on 16
workers; a crash in mentions' last statement and a killed run each restarted it (one
by hand, ``resume_steps.py``). Here ``python3 -m ontology.step_layer_workers build ...``
runs ``ontology.step_migrate build`` (the same arguments) with :func:`build_layer`, which
is ``step_migrate.build_layer`` with checkpoints (:data:`BUILD_LAYER_DIGEST` pins the
code it follows; a change there fails ``ontology.tests.test_step_layer_workers`` until
this copy follows it):

* after the input (the served rows and the unit layer copied, the re-derived values
  dropped, the guard laid, the statistics made) the input file is kept to resume
  (``StoreBuilder.keep_to_resume``: synced, a rollback journal from then on) with the
  build's state (the served links held for later steps, the link types each step
  writes, the report);
* after each step (or side-by-side wave) is finished (its links in, its held links
  shown), again. A stopped build is rolled back to its last checkpoint, never to a
  half-written step.

A rerun of the same command resumes: the input is not copied again (its Postgres
snapshot is the checkpoint's) and the steps done are not run again. The delta mark and
sequence are the first run's. A checkpoint of other code, plan or captures is refused
and kept; ``--fresh`` discards it. ``--resume-dir`` (default ``OUT.resume``).

Disk (11-09): ``--park-unit-layer`` parks the unit layer compressed once the input holds
it (``ontology.unit_park``; the launcher restores it before the apply), and the held
keys nothing reads without the tripwire are not copied (``ontology.held_keys``): on the
1% sample the held keys were half the input, the layer file 5x its parked size.
Disk (11-10): with ``--park-unit-layer`` the unit layer is staged (``ontology.unit_stage``:
its rows read once into compressed files in the order the input takes them) and parked
before the served rows are copied, so the layer and its copy are never whole on disk
together (the full-scale run held a 90 GB layer, an 83 GB input and a 78 GB sort temp of
the layer's links, read through the view's NULL rowid, and stopped at 8.7 GB free). An
input no checkpoint holds (a stopped copy's) is removed when a build starts afresh.
CPU (11-10): the input's links are added through ``ontology.link_pack.LinkWriter``, their
texts packed by a pool of processes (the full-scale copy of ~48M links had spent ~2.5 h
at one core packing them); the same batches, rows and ids as ``builder.add``.
The input is guarded by ``ontology.input_guard`` (``guard_inputs``'s store, its deletes
through the ``object_values`` and ``links`` views never copied into a temp first: the
full-scale build held a 97 GB temp there and its disk guard stopped it).

Why a module of its own: ``ontology.step_migrate`` is derivation code (the kept fields'
key) and step code (the plan's digests); nothing either covers imports this module.

Stdlib only (psycopg through ``ontology.step_migrate``).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import pickle
import sqlite3
import sys
import time
from typing import Any, Iterable, Iterator, Mapping, Sequence

from ontology import input_guard, link_pack, step_migrate as sm

# sha256 of ``inspect.getsource(step_migrate.build_layer)`` this module follows.
# (30a1202e23: main's text-marks re-marking, ported below with its marked set checkpointed)
BUILD_LAYER_DIGEST = "30a1202e23bd312393e082f0b96073c59a39752fb5b53b80059c54e83f2e7d74"
FORMAT = "plx-step-resume/v1"


class ResumeRefused(RuntimeError):
    """A checkpoint that cannot be resumed (another build holds it, or it was made by
    other code, plan or input): never resumed from, never removed."""


def source_digest() -> str:
    import inspect
    return hashlib.sha256(inspect.getsource(sm.build_layer).encode()).hexdigest()


class Checkpoints:
    """One step layer build's checkpoints in ``directory`` (``0700``, locked)."""

    def __init__(self, directory: str | Path, identity: Mapping[str, Any], *,
                 fresh: bool = False) -> None:
        import fcntl
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock = open(self.dir / "lock", "a+")
        try:
            fcntl.flock(self._lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lock.close()
            raise ResumeRefused("resume_dir_in_use") from None
        self.identity = json.loads(json.dumps(dict(identity), sort_keys=True, default=str))
        self.token = hashlib.sha256(json.dumps(self.identity, sort_keys=True).encode()
                                    ).hexdigest()[:16]
        held = self._read()
        if held is not None and not fresh:
            mine = held.get("identity") or {}
            differs = sorted(k for k in set(mine) | set(self.identity)
                             if mine.get(k) != self.identity.get(k))
            if held.get("format") != FORMAT:
                differs = ["format"]
            if differs:
                self._lock.close()
                raise ResumeRefused("resume_identity_differs:" + ",".join(differs))
        if held is None or fresh:
            if held is not None and held.get("input"):
                for suffix in ("", "-journal"):
                    Path(held["input"] + suffix).unlink(missing_ok=True)
            for path in self.dir.iterdir():
                if path.name != "lock":
                    path.unlink()
            held = {"format": FORMAT, "identity": self.identity, "phase": None, "values": {}}
        self.manifest = held

    @property
    def phase(self) -> str | None:
        return self.manifest.get("phase")

    def _read(self) -> dict[str, Any] | None:
        try:
            return json.loads((self.dir / "manifest.json").read_text())
        except FileNotFoundError:
            return None
        except ValueError:
            raise ResumeRefused("resume_manifest_unreadable") from None

    def _write(self) -> None:
        from ontology.unit_layer_fit import _atomic
        _atomic(self.dir / "manifest.json",
                json.dumps(self.manifest, sort_keys=True, indent=1).encode())

    def value(self, name: str, compute: Any) -> Any:
        values = self.manifest.setdefault("values", {})
        if name not in values:
            values[name] = compute()
            self._write()
        return values[name]

    def keep(self, builder: Any, phase: str, state: Mapping[str, Any]) -> None:
        """Commit the input as ``phase`` with the build's ``state``."""
        from ontology.unit_layer_fit import _atomic
        conn = builder.connection
        open_before = conn.in_transaction
        if not open_before:
            conn.execute("BEGIN IMMEDIATE")
        if not builder.keep_to_resume({"id": self.token, "phase": phase}):
            raise ResumeRefused("input_cannot_checkpoint")
        if not open_before:   # as the build stood: outside a transaction
            conn.execute("COMMIT")
        _atomic(self.dir / "state.pickle", pickle.dumps(dict(state),
                                                        protocol=pickle.HIGHEST_PROTOCOL))
        self.manifest.update(phase=phase, input=str(builder.temp))
        self._write()

    def state(self) -> dict[str, Any]:
        return pickle.loads((self.dir / "state.pickle").read_bytes())

    def reopen(self, target: Path) -> Any:
        """The input kept to resume, rolled back to its last checkpoint, as a builder
        (its tables are the guard's views by now: the store DDL is not run again)."""
        from ontology import store as S
        temp = Path(self.manifest["input"])
        if not temp.is_file():
            raise ResumeRefused("input_missing")
        builder = S.StoreBuilder.__new__(S.StoreBuilder)
        builder.target, builder.temp = Path(target), temp
        builder.packed_bindings, builder._shared = frozenset(), {}
        builder.seed_mode, builder._part, builder._seed_parts = "resumed", True, None
        builder.digest_mode = builder.digest_segments = None
        builder.seconds, builder.digest_workers = {}, 1
        builder.kept = True
        conn = sqlite3.connect(str(temp), isolation_level=None)
        conn.execute("PRAGMA journal_mode = DELETE")   # a stopped step rolls back
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute(f"PRAGMA cache_size = -{max(2048, S.PART_CACHE_BYTES // 1024)}")
        conn.execute("PRAGMA foreign_keys = OFF")
        row = conn.execute("SELECT value FROM store_meta WHERE key = ?",
                           (S.CHECKPOINT_META,)).fetchone()
        held = json.loads(row[0]) if row else {}
        if held.get("id") != self.token or held.get("phase") != self.phase:
            conn.close()
            raise ResumeRefused("input_checkpoint_differs")
        builder.checkpoint = held
        builder._conn = conn
        return builder

    def finish(self) -> None:
        held = self.manifest.get("input")
        if held:
            for suffix in ("", "-journal"):
                Path(held + suffix).unlink(missing_ok=True)
        self._lock.close()
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def close(self) -> None:
        self._lock.close()


_ACTIVE: dict[str, Any] = {}   # the build's Checkpoints while ``wired``


def build_layer(the_plan: Mapping[str, Any], new: Any, steps: Mapping[str, Any], dsn: str,
                out: str | Path, *, sources: Mapping[str, Any] | None = None,
                unit_layer: str | Path | None = None, normalizer: Any = None,
                delta_mark: int = 0, specs: Mapping[str, Any] = sm.STEPS,
                sequence: int | None = None, workers: int | None = None,
                steps_side_by_side: int | None = None,
                checkpoints: Checkpoints | None = None) -> dict[str, Any]:
    """``ontology.step_migrate.build_layer`` (the same layer, row for row) with a
    checkpoint after its input and after each finished step or wave (module docstring).
    ``checkpoints`` None: the active build's (``wired``), else none."""
    from ontology.project import open_generation
    from ontology.schema_migrate import read_layer
    from ontology.store import StoreBuilder, CHECKPOINT_META
    from ontology import store_ddl

    keep = checkpoints if checkpoints is not None else _ACTIVE.get("checkpoints")
    cache = _ACTIVE.get("mention_cache")
    if cache and "mentions" in steps:   # its mentions kept for the next migration
        from ontology.mention_cache import cached_step
        steps = {**steps, "mentions": cached_step(steps["mentions"], cache)}
    run = dict(the_plan["steps"]["run"])
    remarks = set(the_plan["steps"].get("remark") or ())   # re-marked in place
    marked: set[str] = set()
    if the_plan["steps"]["refused"]:
        raise ValueError("step_migration_refused:" + ",".join(the_plan["steps"]["refused"]))
    missing = sorted(set(run) - set(steps))
    if missing:
        raise ValueError("steps_missing:" + ",".join(missing))
    sources = dict(sources or {})
    from ontology import unit_park
    unit = unit_park.record(unit_layer) if unit_layer is not None else None
    units = the_plan.get("units") or {}
    rewrites = (units.get("units") or {}).get("affected") or \
        (units.get("units") or {}).get("dropped")
    if unit is None and rewrites:
        raise ValueError("unit_layer_required")
    if unit is not None and unit.get("plan") != units.get("id"):
        raise ValueError("unit_layer_of_another_plan")
    inputs = sm.declared_inputs(run, (new,), specs)
    owned = [o for name in sorted(run) for o in sm._owned(name, run[name], specs)]
    values_owned = sm._owned_values(run, specs)
    values_where, values_params = sm.owned_values_where(values_owned)
    order = sorted(steps)
    first = min(run) if run else None
    started = time.monotonic()
    out = Path(out)
    out.unlink(missing_ok=True)
    work = out.with_name(f".{out.name}.input")
    resuming = keep is not None and keep.phase is not None
    if not resuming:
        work.unlink(missing_ok=True)
        if keep is not None:
            # an input no checkpoint holds (a build stopped while copying, its file left
            # by a killed process; 83 GB on 11-10) is this build's own: it goes
            for stale in work.parent.glob(f".{work.name}.build-*"):
                stale.unlink(missing_ok=True)
    from ontology import unit_stage
    stage = unit_stage.stage_dir(out)
    staged = (_ACTIVE.get("park_unit") and unit_layer is not None and keep is not None
              and not resuming)
    report: dict[str, Any] = {"id": the_plan["id"], "steps": {}, "run": run}
    held: dict[str, list[dict[str, Any]]] = {}
    type_steps: dict[str, set[str]] = {}
    shown = dropped = 0
    i = 0
    indexed = False
    builder = keep.reopen(work) if resuming else StoreBuilder(work, foreign_keys=False)
    kept_ok = False
    try:
        open_generation(builder)
        conn = builder.connection
        if resuming:
            state = keep.state()
            report, held, type_steps = state["report"], state["held"], state["type_steps"]
            i, indexed = state["next"], state["indexed"]
            # the documents a finished re-marking step found (never lost on a resume:
            # an empty set would apply as "nothing to mark")
            marked = set(state.get("marked") or ())
            report["resumed"] = {"phase": keep.phase, "next": i}
        else:
            pending: list[dict[str, Any]] = []
            for step_name, spec in specs.items():
                for t in (spec.writes(new) if spec.writes is not None else ()):
                    type_steps.setdefault(t, set()).add(step_name)

            def take(rows: Iterable[dict[str, Any]]) -> None:
                nonlocal shown, dropped
                for row in rows:
                    if sm._is_owned(row["link_id"], row["from_object"], owned):
                        dropped += 1           # the step derives it again
                        owner = sm.step_of(row["link_id"], specs)
                        if owner is not None:
                            type_steps.setdefault(row["link_type"], set()).add(owner)
                        continue
                    name = sm.step_of(row["link_id"], specs)
                    if name is not None:
                        type_steps.setdefault(row["link_type"], set()).add(name)
                    if name is not None and first is not None and name >= first:
                        held.setdefault(name, []).append(row)
                        continue
                    pending.append(row)
                    shown += 1
                    if len(pending) >= 20_000:
                        links.add(pending)   # the batch add("links", ...) was given
                        pending.clear()

            if staged:
                # the unit layer read once into its stage and parked before the served
                # rows come in: never whole on disk beside its copy (ontology.unit_stage)
                got = unit_stage.prepare(unit_layer, builder, stage, workers=workers)
                report["unit_stage"] = {k: got[k] for k in ("counts", "bytes", "seconds")
                                        if k in got}
                report["unit_park"] = got["park"]
                print(f"unit layer staged and parked: {json.dumps(report['unit_stage'])} "
                      f"{json.dumps(got['park'])}", file=sys.stderr, flush=True)
            # the links' texts packed on every core, added in order (ontology.link_pack)
            with link_pack.LinkWriter(builder, sm._workers(workers)) as links:
                take(sm.copy_served(dsn, builder, types=inputs, unit=unit,
                                    workers=workers))
                if staged:
                    take(unit_stage.copy_staged(stage, builder,
                                                unit_id=unit.get("id") if unit else None))
                elif unit_layer is not None:
                    take(sm.copy_unit_layer(unit_layer, builder, workers=workers))
                if pending:
                    links.add(pending)
                    pending.clear()
                print(f"links added: {links.flush()} (texts packed in "
                      f"{'a pool' if links.parallel else 'this process'})",
                      file=sys.stderr, flush=True)
            values_dropped = 0
            if values_owned:
                values_dropped = conn.execute("SELECT count(*) FROM object_values WHERE "
                                              + values_where, values_params).fetchone()[0]
                conn.execute("DELETE FROM object_values WHERE " + values_where, values_params)
            report["input"] = {"links_shown": shown, "links_rederived": dropped,
                               "values_rederived": values_dropped,
                               "links_held": sum(len(v) for v in held.values()),
                               "types": inputs,
                               "seconds": round(time.monotonic() - started, 3)}
            conn.commit()
            # the same guarded input, its moved rows never copied whole into a temp
            # (ontology.input_guard: a 97 GB temp stopped the full-scale build, 11-10)
            t = time.monotonic()
            how = input_guard.guard_inputs(
                conn, inputs, [p for spec in specs.values() for p in spec.prefixes])
            print(f"input guarded by {how} in {time.monotonic() - t:.0f} s",
                  file=sys.stderr, flush=True)
            sm.analyze_input(conn)

        names = [n for n in order if first is not None and n >= first]

        def checkpoint(phase: str) -> None:
            nonlocal kept_ok
            if keep is None:
                return
            keep.keep(builder, phase, {"report": report, "held": held,
                                       "type_steps": type_steps, "next": i,
                                       "indexed": indexed, "marked": sorted(marked)})
            kept_ok = True

        if not resuming:
            checkpoint("input")
        else:
            kept_ok = True
        if kept_ok:
            unit_stage.remove(stage)   # taken whole into the input checkpointed
        if _ACTIVE.get("park_unit") and unit_layer is not None and kept_ok and not staged:
            # read whole into the input by now and not again until the apply: parked
            # (``ontology.unit_park``) while the steps work, restored before the apply
            report["unit_park"] = unit_park.park(unit_layer)
            print(f"unit layer parked: {json.dumps(report['unit_park'])}", file=sys.stderr,
                  flush=True)

        def index_for(step_names: Iterable[str]) -> None:
            nonlocal indexed
            if not indexed and any(specs[n].reads for n in step_names):
                indexed = True
                t = time.monotonic()
                report["read_indexes"] = {"made": sm.read_indexes(conn),
                                          "seconds": round(time.monotonic() - t, 3)}

        def run_here(name: str, mode: str = "here") -> None:
            t = time.monotonic()
            spec = specs[name]
            index_for([name])
            srcs = sm._sources_of(new, sources, run[name]) if spec.documents else sources
            projector = sm._projector(new, srcs, normalizer)
            if name in remarks:   # what it marks, never its rows written again
                def call(_builder: Any, projector: Any) -> dict[str, Any]:
                    found, counts = sm.text_marks(projector)
                    marked.update(found)
                    return counts
            else:
                call = steps[name]
            try:
                result = call(builder, projector)
            except sqlite3.OperationalError as exc:
                if sm.UNDECLARED in str(exc):
                    raise sm.UndeclaredRead(f"step_read_undeclared_type:{name}") from exc
                raise
            except Exception as exc:
                if sm.UNDECLARED in str(exc) or isinstance(exc.__cause__, sqlite3.Error) \
                        and sm.UNDECLARED in str(exc.__cause__):
                    raise sm.UndeclaredRead(f"step_read_undeclared_type:{name}") from exc
                raise
            conn.commit()
            report["steps"][name] = {"seconds": round(time.monotonic() - t, 3),
                                     "report": result, "mode": mode}

        def finish(name: str) -> None:
            if name in run and run[name] is not None:
                froms = [f"{t}:" for t in run[name]]
                for p in specs[name].prefixes:
                    conn.execute(
                        "DELETE FROM link_rows WHERE link_id >= ? AND link_id < ? AND "
                        "NOT (" + " OR ".join("substr(from_object, 1, ?) = ?"
                                              for _ in froms) + ")",
                        (p, sm.prefix_end(p),
                         *[x for f in froms for x in (len(f), f)]))
            rows = held.pop(name, [])
            for k in range(0, len(rows), 20_000):
                builder.add("links", rows[k:k + 20_000])
            conn.commit()

        side = sm._step_concurrency(steps_side_by_side)
        while i < len(names):
            wave, j = sm._wave(names, i, run, specs, side)
            if len(wave) < 2 or remarks & set(wave):
                if names[i] in run:
                    run_here(names[i])
                finish(names[i])
                i += 1
                checkpoint(f"step:{names[i - 1]}")
                continue
            index_for(wave)
            conn.commit()
            with sm._part_folder(builder) as folder:
                done = sm.run_side_by_side(wave, steps, new, sources, normalizer,
                                           str(builder.temp), Path(folder), run, specs,
                                           type_steps)
                for name in names[i:j]:
                    if name in done and done[name][0] == "ok":
                        _, result, part, seconds = done[name]
                        t = time.monotonic()
                        from ontology.project_parallel import merge_link_parts
                        merge_link_parts(builder, [Path(part)], rebuild_indexes=False)
                        report["steps"][name] = {
                            "seconds": round(seconds + time.monotonic() - t, 3),
                            "report": result, "mode": "side_by_side"}
                    elif name in run:
                        run_here(name, "here:" + (done[name][1] if name in done else "-"))
                    finish(name)
            i = j
            checkpoint(f"step:{names[j - 1]}")
        value_rows: list[dict[str, Any]] = []
        value_objects: list[dict[str, Any]] = []
        value_versions: list[dict[str, Any]] = []
        if values_owned:
            value_rows = [dict(zip(store_ddl.VALUE_COLUMNS, r)) for r in conn.execute(
                f"SELECT {', '.join(store_ddl.VALUE_COLUMNS)} FROM object_values WHERE "
                + values_where + " ORDER BY object_id, l1_handle, binding_id, property",
                values_params)]
            ids = json.dumps(sorted({r["object_id"] for r in value_rows}))
            for table, sink in (("objects", value_objects),
                                ("object_versions", value_versions)):
                cols = builder._INSERT[table]
                sink += [dict(zip(cols, r)) for r in conn.execute(
                    f"SELECT {', '.join(cols)} FROM {table} WHERE object_id IN "
                    "(SELECT value FROM json_each(?)) ORDER BY rowid", (ids,))]
        conn.commit()
        t = time.monotonic()
        layer = StoreBuilder(out, foreign_keys=False)
        try:
            open_generation(layer)
            with sm._part_folder(builder) as folder:
                links, ends = sm.write_layer_links(layer, conn, builder.temp, owned,
                                                   sm._workers(workers), Path(folder))
            for table, rows in (("objects", value_objects),
                                ("object_versions", value_versions),
                                ("object_values", value_rows)):
                for k in range(0, len(rows), 20_000):
                    layer.add(table, rows[k:k + 20_000])
            layer.set_meta("format", sm.FORMAT)
            layer.set_meta("steps_digest", the_plan["steps_to"])
            c = layer.connection
            if remarks:
                c.execute(f"CREATE TABLE {sm.TEXT_MARKS} (version_handle TEXT PRIMARY KEY)")
                c.executemany(f"INSERT INTO {sm.TEXT_MARKS} VALUES (?)",
                              [(h,) for h in sorted(marked)])
            scope = sorted(ends | {p for o in owned if o["from"] for p in o["from"]}
                           | {o["prefix"] for o in values_owned}
                           | set(sm._text_marks_scope(dsn, sorted(marked)) if remarks else ()))
            from ontology.schema_migrate import content_digest
            content = content_digest(c, ("link_rows", "objects", "object_versions",
                                         "value_sources", "value_cells")
                                     + ((sm.TEXT_MARKS,) if remarks else ()))
            sequence = int(time.time() * 1000) if sequence is None else int(sequence)
            unit_id = unit["id"] if unit is not None else None
            record = {"id": f"{sm._sha([the_plan['id'], unit_id, content, sequence])[:16]}"
                            f"-{sequence}",
                      "plan": the_plan["id"], "unit": unit_id, "content": content,
                      "sequence": sequence, "from": the_plan["steps_from"],
                      "to": the_plan["steps_to"], "owned": owned, "values": values_owned,
                      "scope": scope,
                      "steps": sorted(run), "inputs": inputs, "delta_mark": int(delta_mark),
                      "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            if remarks:   # the apply adds ``marks`` to the text fields of these documents
                record["text_marks"] = {"steps": sorted(remarks),
                                        "marks": list(sm.SENSITIVE_MARKS),
                                        "documents": len(marked)}
            report["id"] = record["id"]
            c.execute(f"CREATE TABLE {sm.TABLE} (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            c.executemany(f"INSERT INTO {sm.TABLE} VALUES (?, ?)",
                          [(k, json.dumps(v, sort_keys=True)) for k, v in record.items()])
            path = layer.keep_as_part()
        except BaseException:
            layer.abort()
            raise
        report["layer_seconds"] = round(time.monotonic() - t, 3)
    except BaseException:
        if not kept_ok:
            builder.kept = False   # nothing kept to resume from: the input goes
        builder.abort()
        raise
    builder.kept = False
    builder.abort()
    for suffix in ("", "-wal", "-shm", "-journal"):
        Path(f"{work}{suffix}").unlink(missing_ok=True)
    path.replace(out)
    if remarks:
        report["text_marks"] = len(marked)
    report.update(links=links, values=len(value_rows), scope=scope,
                  seconds=round(time.monotonic() - started, 3), store=str(out))
    return report


def identity(the_plan: Mapping[str, Any], state: Any, out: Any, unit_layer: Any) -> dict:
    """What a step layer build's checkpoints depend on."""
    from ontology import project
    unit = None
    if unit_layer is not None:
        from ontology.unit_park import record
        unit = record(unit_layer).get("id")
    return {"derivation_code": project.derivation_code(), "plan": the_plan["id"],
            "steps_from": the_plan["steps_from"], "steps_to": the_plan["steps_to"],
            "code": source_digest(), "unit_layer": unit,
            "state": str(Path(state).resolve()), "out": str(Path(out).resolve())}


@contextlib.contextmanager
def wired(directory: str | None, fresh: bool, mention_cache: str | None = None,
          park_unit: bool = False) -> Iterator[dict[str, Any]]:
    """While open, ``ontology.step_migrate build`` builds with :func:`build_layer` and
    checkpoints in ``directory`` (None: ``OUT.resume``), resuming from them; with
    ``mention_cache`` (a file, ``ontology.mention_cache``) its ``mentions`` step takes
    and keeps the extracted mentions there."""
    from operating.pg import ontology_migrate
    from ontology import schema_migrate
    box: dict[str, Any] = {}
    saved = (sm.build_from_state, sm.build_layer, ontology_migrate.delta_mark,
             schema_migrate.next_sequence)

    def build_from_state(the_plan: Any, new: Any, state: Path, out: Path, **kwargs: Any):
        held = Checkpoints(directory or f"{out}.resume",
                           identity(the_plan, state, out, kwargs.get("unit_layer")),
                           fresh=fresh)
        box["checkpoints"] = _ACTIVE["checkpoints"] = held
        print(f"checkpoints: {held.dir} ({held.phase or 'none yet'})", file=sys.stderr,
              flush=True)
        return saved[0](the_plan, new, state, out, **kwargs)

    def delta_mark(dsn: str) -> int:
        held = _ACTIVE.get("checkpoints")
        return held.value("delta_mark", lambda: saved[2](dsn)) if held else saved[2](dsn)

    def next_sequence(dsn: str) -> int:
        held = _ACTIVE.get("checkpoints")
        return held.value("sequence", lambda: saved[3](dsn)) if held else saved[3](dsn)

    if mention_cache:
        _ACTIVE["mention_cache"] = mention_cache
    if park_unit:
        _ACTIVE["park_unit"] = True
    sm.build_from_state, sm.build_layer = build_from_state, build_layer
    ontology_migrate.delta_mark, schema_migrate.next_sequence = delta_mark, next_sequence
    from ontology.held_keys import skipped   # no held key copied that nothing reads
    try:
        with skipped():
            yield box
    finally:
        (sm.build_from_state, sm.build_layer, ontology_migrate.delta_mark,
         schema_migrate.next_sequence) = saved
        _ACTIVE.pop("checkpoints", None)
        _ACTIVE.pop("mention_cache", None)
        _ACTIVE.pop("park_unit", None)


def _cache_arg(args: list[str]) -> tuple[list[str], str | None]:
    """``args`` without ``--mention-cache FILE``, and FILE (else ``PLX_MENTION_CACHE``)."""
    from ontology.mention_cache import ENV
    rest, found = [], os.environ.get(ENV) or None
    items = iter(args)
    for item in items:
        if item == "--mention-cache":
            found = next(items, None)
            if not found:
                raise SystemExit("--mention-cache needs a file")
        elif item.startswith("--mention-cache="):
            found = item.split("=", 1)[1]
        else:
            rest.append(item)
    return rest, found


def main(argv: Sequence[str] | None = None) -> int:
    from ontology.unit_layer_workers import _resume_args
    args, options = _resume_args(list(sys.argv[1:] if argv is None else argv))
    args, cache = _cache_arg(args)
    park_unit = "--park-unit-layer" in args
    args = [a for a in args if a != "--park-unit-layer"]
    if args[:1] == ["build"]:
        from ontology.build_budget import people_guard
        unavailable = people_guard()
        if unavailable:
            print(f"refused: people exclusion unavailable ({unavailable}): the people "
                  "steps would link no one", file=sys.stderr)
            return 4
    if park_unit and (args[:1] != ["build"] or options["off"]):
        print("refused: --park-unit-layer needs a checkpointed build (a stopped build "
              "resumes after the input, never copying the parked layer again)", file=sys.stderr)
        return 2
    if args[:1] != ["build"] or (options["off"] and not cache):
        return sm.main(args)
    if options["off"]:   # no checkpoints: this module's build_layer for the cache only
        saved = sm.build_layer
        _ACTIVE["mention_cache"], sm.build_layer = cache, build_layer
        try:
            return sm.main(args)
        finally:
            sm.build_layer = saved
            _ACTIVE.pop("mention_cache", None)
    with wired(options["dir"], options["fresh"], cache, park_unit) as box:
        code = sm.main(args)
    held = box.get("checkpoints")
    if held is not None:
        if code == 0:
            held.finish()
        else:
            held.close()
    return code


if __name__ == "__main__":
    sys.exit(main())
