"""The unit migration layer staged for the step input: read once into compressed files
in the order the input takes them, then parked, so the layer file and the input's copy
of it are never on disk together.

Why (measured 11-10 on the live host): the step build copied the 90 GB unit layer into
its input (``step_migrate.copy_unit_layer``) with the layer whole beside it, and the
layer's links were read ``ORDER BY rowid`` through the ``links`` view, whose rowid is
NULL: SQLite sorted every decoded link in a temp file (78 GB) to give back the scan
order. Input 83 GB + layer 90 GB + that temp left /data at 8.7 GB.

Here, before the served rows are copied: the layer's rows are written to ``DIR`` (the
plain tables' rows and its links as pickled batches through ``zstd``; its values as the
same ``ValuePart`` files ``copy_unit_layer`` makes, each compressed by the worker that
made it), the stage is recorded (``stage.json``, written last), and the layer is parked
(``ontology.unit_park``). The input then takes the staged rows (:func:`copy_staged`)
exactly as ``copy_unit_layer`` gives them: the same tuples in the same order, the same
part files in the same order, the links in the order of ``link_rows``'s rowid (the scan
order the old sort gave back: SQLite's sorter keeps equal keys in input order), each
staged file removed once taken. The links are read from ``link_rows`` with the layer's
own ``links`` view expressions; a layer whose view is not a plain ``SELECT ... FROM
link_rows l`` is refused.

A stage is used only whole and only for the layer it was made from (its record id); a
stage being taken is no longer whole (``stage.json`` is removed first), so a build
stopped while taking it stages again (restoring the parked layer first).

Not derivation code: nothing the fields key or a plan digest covers imports it (only
``ontology.step_layer_workers``). Stdlib only; needs the ``zstd`` command.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import pickle
import re
import shutil
import sqlite3
import subprocess
import time
from typing import Any, Iterator

from ontology import step_migrate as sm
from ontology import unit_park

FORMAT = "plx-unit-stage/v1"
TABLES = ("objects", "object_versions", "object_removals", "identifiers")
BATCH = 20_000
_VIEW = re.compile(r"\s*CREATE\s+VIEW\s+(?:IF\s+NOT\s+EXISTS\s+)?links\s+AS\s+(SELECT\s.*\s"
                   r"FROM\s+link_rows\s+l)\s*", re.S | re.I)


class StageError(RuntimeError):
    """A stage that cannot be made or taken as recorded."""


def stage_dir(out: str | Path) -> Path:
    """Where the step layer ``out``'s unit stage is made: beside it (its disk)."""
    p = Path(out)
    return p.with_name(f".{p.name}.unit-stage")


def _zstd() -> str:
    found = shutil.which("zstd")
    if not found:
        raise StageError("zstd_missing")
    return found


def _write_stream(path: Path, batches: Iterator[list[tuple[Any, ...]]]) -> int:
    """Pickle each batch through ``zstd`` into ``path``; returns the rows written."""
    proc = subprocess.Popen([_zstd(), "-q", "-T0", "-3", "--check", "-f", "-o", str(path)],
                            stdin=subprocess.PIPE)
    count = 0
    try:
        assert proc.stdin is not None
        for batch in batches:
            pickle.dump(batch, proc.stdin, protocol=pickle.HIGHEST_PROTOCOL)
            count += len(batch)
        proc.stdin.close()
    except BaseException:
        proc.kill()
        proc.wait()
        raise
    if proc.wait() != 0:
        raise StageError(f"zstd_failed:{path.name}")
    return count


def _read_stream(path: Path) -> Iterator[list[tuple[Any, ...]]]:
    proc = subprocess.Popen([_zstd(), "-q", "-d", "-c", str(path)], stdout=subprocess.PIPE)
    try:
        assert proc.stdout is not None
        while True:
            try:
                batch = pickle.load(proc.stdout)
            except EOFError:
                break
            yield batch
    except BaseException:
        proc.kill()
        raise
    finally:
        if proc.stdout is not None:
            proc.stdout.close()
        code = proc.wait()
    if code != 0:
        raise StageError(f"zstd_failed:{path.name}")


def _batches(cursor: Any) -> Iterator[list[tuple[Any, ...]]]:
    while True:
        batch = cursor.fetchmany(BATCH)
        if not batch:
            return
        yield [tuple(r) for r in batch]


def links_sql(conn: sqlite3.Connection) -> str:
    """The layer's links, as its ``links`` view gives them, in ``link_rows`` rowid order
    (the view's own rowid is NULL: ordering by it sorts every row to give this order)."""
    from ontology import store_ddl
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'view' AND name = 'links'"
                       ).fetchone()
    found = _VIEW.fullmatch(row[0]) if row else None
    if found is None:
        raise StageError("links_view_unexpected")
    sql = f"{found.group(1)} ORDER BY l.rowid"
    names = [d[0] for d in conn.execute(f"{sql} LIMIT 0").description]
    if names != list(store_ddl.LINK_COLUMNS):
        raise StageError("links_view_columns_unexpected")
    return sql


def _staged_value_part(path: str, layer: str, low: str | None, high: str | None) -> str:
    """A worker's part (``step_migrate._layer_value_part``), compressed; its path."""
    made = Path(sm._layer_value_part(path, layer, low, high))
    out = made.with_name(made.name + ".zst")
    subprocess.run([_zstd(), "-q", "-1", "--check", "-f", "--rm", "-o", str(out), str(made)],
                   check=True)
    return str(out)


def _all_parts(jobs: list[tuple[Any, ...]], workers: int, folder: Path) -> list[str]:
    """Each job's part (:func:`_staged_value_part`) by ``workers`` processes, every job
    queued at once (a part waits compressed, so no window bounds them: one slow range
    never idles the rest); their names in the order of ``jobs``."""
    names = [f"part-{i:06d}.sqlite" for i in range(len(jobs))]
    if workers <= 1:
        return [Path(_staged_value_part(str(folder / name), *args)).name
                for name, args in zip(names, jobs)]
    import concurrent.futures as cf
    import multiprocessing
    from ontology.store import main_not_imported
    with main_not_imported():
        pool = cf.ProcessPoolExecutor(workers, mp_context=multiprocessing.get_context("spawn"))
    try:
        futures = [pool.submit(_staged_value_part, str(folder / name), *args)
                   for name, args in zip(names, jobs)]
        return [Path(f.result()).name for f in futures]
    finally:
        pool.shutdown(wait=True, cancel_futures=True)


def read(directory: str | Path) -> dict[str, Any] | None:
    """The record of a whole stage in ``directory`` (None: none, or not whole)."""
    try:
        held = json.loads((Path(directory) / "stage.json").read_text())
    except FileNotFoundError:
        return None
    except ValueError:
        return None
    return held if held.get("format") == FORMAT else None


def _fsync_dir(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def make(layer: str | Path, builder: Any, directory: str | Path, *,
         workers: int | None = None) -> dict[str, Any]:
    """Stage the unit layer at ``layer`` (held whole) into ``directory`` (replaced) for
    ``builder`` (the step input: whether its values go in by parts is its own, as
    ``copy_unit_layer`` decides it); returns the stage's record."""
    from ontology import store as S
    started = time.monotonic()
    d = Path(directory)
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, mode=0o700)
    layer = Path(layer)
    rec = unit_park.record(layer)
    n = sm._workers(workers)
    by_parts = sm._by_parts(builder, n)
    counts: dict[str, int] = {}
    cols = builder._INSERT

    def rows_and_links() -> None:
        # its own connection, beside the value parts' workers (sqlite3 steps without the
        # GIL): the links' read is one long sequential pass (48M links, ~17 min, 11-10)
        own = sqlite3.connect(f"file:{layer}?mode=ro", uri=True)
        try:
            for table in TABLES:
                counts[table] = _write_stream(d / f"{table}.pkl.zst", _batches(own.execute(
                    f"SELECT {', '.join(cols[table])} FROM {table} ORDER BY rowid")))
            counts["links"] = _write_stream(d / "links.pkl.zst",
                                            _batches(own.execute(links_sql(own))))
        finally:
            own.close()

    import concurrent.futures as cf
    parts: list[str] = []
    conn = sqlite3.connect(f"file:{layer}?mode=ro", uri=True)
    try:
        links_sql(conn)   # an unexpected links view refused before any work
        with cf.ThreadPoolExecutor(1) as side:
            beside = side.submit(rows_and_links)
            try:
                if by_parts:
                    (count,) = conn.execute("SELECT count(*) FROM objects").fetchone()
                    bounds = S._object_bounds(conn, max(1, -(-count // (
                        n * sm._VALUE_PARTS_PER_WORKER))))
                    jobs = [(str(layer), lo, hi) for lo, hi in zip(bounds, bounds[1:])]
                    folder = d / "values"
                    folder.mkdir()
                    parts = _all_parts(jobs, n, folder)
                else:
                    counts["object_values"] = _write_stream(d / "object_values.pkl.zst",
                                                            S._value_rows(conn))
            finally:
                beside.result()   # its failure raised here (after the parts' own)
    finally:
        conn.close()
    held = {"format": FORMAT, "unit": rec.get("id"), "workers": n, "by_parts": by_parts,
            "parts": parts, "counts": counts,
            "bytes": sum(p.stat().st_size for p in d.rglob("*") if p.is_file()),
            "seconds": round(time.monotonic() - started, 3)}
    for p in d.rglob("*"):
        if p.is_file():
            unit_park._fsync(p)
    tmp = d / "stage.json.part"
    tmp.write_text(json.dumps(held, sort_keys=True))
    unit_park._fsync(tmp)
    tmp.replace(d / "stage.json")
    _fsync_dir(d)
    return held


def prepare(layer: str | Path, builder: Any, directory: str | Path, *,
            workers: int | None = None) -> dict[str, Any]:
    """A whole stage of the unit layer at ``layer`` in ``directory`` (kept when whole and
    of this layer, else made again, restoring a parked layer first), then the layer
    parked. Returns the stage's record with the park's report."""
    rec = unit_park.record(layer)
    held = read(directory)
    if held is not None and (held.get("unit") != rec.get("id")
                             or held.get("workers") != sm._workers(workers)
                             or held.get("by_parts") != sm._by_parts(builder,
                                                                    sm._workers(workers))):
        held = None
    restored = None
    if held is None:
        if unit_park.parked(layer) is not None:
            restored = unit_park.restore(layer)
        held = make(layer, builder, directory, workers=workers)
    out = dict(held)
    out["park"] = unit_park.park(layer)
    if restored is not None:
        out["restored"] = restored
    return out


def copy_staged(directory: str | Path, builder: Any, *, unit_id: str | None
                ) -> Iterator[dict[str, Any]]:
    """``step_migrate.copy_unit_layer`` from a whole stage (:func:`prepare`): the same
    rows into ``builder``, the same links yielded in the same order. The stage stops
    being whole as it is taken; each file goes once taken."""
    from ontology import store_ddl
    d = Path(directory)
    held = read(d)
    if held is None:
        raise StageError("stage_not_whole")
    if held.get("unit") != unit_id:
        raise StageError("stage_of_another_unit_layer")
    if held["by_parts"] != sm._by_parts(builder, held["workers"]):
        raise StageError("stage_values_differ")
    (d / "stage.json").unlink()
    _fsync_dir(d)
    for table in TABLES:
        path = d / f"{table}.pkl.zst"
        sm._insert(builder, table, (r for batch in _read_stream(path) for r in batch))
        path.unlink()
    if held["by_parts"]:
        folder = d / "values"

        def parts() -> Iterator[Path]:
            for name in held["parts"]:
                packed = folder / name
                plain = packed.with_name(packed.name[:-len(".zst")])
                subprocess.run([_zstd(), "-q", "-d", "-f", "-o", str(plain), str(packed)],
                               check=True)
                packed.unlink()
                yield plain   # removed by add_value_parts once taken
        builder.add_value_parts(parts())
    else:
        path = d / "object_values.pkl.zst"
        for batch in _read_stream(path):
            builder.add("object_values", [dict(zip(store_ddl.VALUE_COLUMNS, r))
                                          for r in batch])
        path.unlink()
    path = d / "links.pkl.zst"
    for batch in _read_stream(path):
        for r in batch:
            yield dict(zip(store_ddl.LINK_COLUMNS, r))
    path.unlink()


def remove(directory: str | Path) -> None:
    shutil.rmtree(Path(directory), ignore_errors=True)
