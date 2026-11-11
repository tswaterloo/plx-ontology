"""The unit migration layer parked while the step layer works: compressed beside itself
once the step build has copied it in, restored before the apply.

Why: the step build copies the whole unit layer into its input (``step_migrate.
copy_unit_layer``) and never reads the file again, but the apply needs it. Measured on the
live host (lp2, 2026-11-07/08) the layer was 47 GB and compressed about 7x; kept whole it
sat on /data beside the step input for the whole step build (hours), the largest piece of
the build's disk peak the build itself did not need.

``park(path)`` writes ``path.zst`` (zstd, its frame content checksum on), records the
layer's migration record, size and the parked file's size in ``path.parked.json``, then
removes ``path``. ``restore(path)`` decompresses ``path.zst`` to a temporary file
(zstd verifies the checksum), checks its size and record against the parked ones, and
renames it into place; only then are ``path.zst`` and the record removed. A parked layer
is never read as a layer: ``path`` is absent until restored, so an apply finds no file
(fails) rather than a partial one. ``record(path)`` is the layer's migration record,
whether held or parked (the step build's resume and identity read it).

Every step is safe to stop and run again: a stopped park leaves ``path`` whole (the
partial ``.zst.part`` is removed by the next park); a stopped restore leaves ``path.zst``
whole (the partial ``.restore`` file is removed by the next restore).

Not derivation code: nothing the fields key or a plan digest covers imports it.
Stdlib only; needs the ``zstd`` command.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any

PARKED_FORMAT = "plx-unit-park/v1"


class ParkError(RuntimeError):
    """A layer that cannot be parked or restored as recorded (nothing was removed)."""


def _paths(path: str | Path) -> tuple[Path, Path, Path]:
    p = Path(path)
    return p, p.with_name(p.name + ".zst"), p.with_name(p.name + ".parked.json")


def _layer_record(path: Path) -> dict[str, Any]:
    from ontology.schema_migrate import read_layer
    return read_layer(path)


def _fsync(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _zstd() -> str:
    found = shutil.which("zstd")
    if not found:
        raise ParkError("zstd_missing")
    return found


def parked(path: str | Path) -> dict[str, Any] | None:
    """The parked record of ``path`` (None: not parked)."""
    _, zst, meta = _paths(path)
    if not meta.is_file():
        return None
    held = json.loads(meta.read_text())
    if held.get("format") != PARKED_FORMAT or not zst.is_file():
        raise ParkError("parked_record_unreadable")
    return held


def record(path: str | Path) -> dict[str, Any]:
    """The migration record of the unit layer at ``path``, held or parked."""
    p = Path(path)
    if p.is_file():
        return _layer_record(p)
    held = parked(p)
    if held is None:
        raise ParkError(f"unit_layer_missing:{p}")
    return held["record"]


def park(path: str | Path, *, threads: int = 0, level: int = 3) -> dict[str, Any]:
    """Compress the layer at ``path`` to ``path.zst`` and remove it (module docstring);
    a layer already parked: nothing done."""
    p, zst, meta = _paths(path)
    if not p.is_file():
        if parked(p) is not None:
            return {"parked": "already"}
        raise ParkError(f"unit_layer_missing:{p}")
    for suffix in ("-wal", "-journal"):
        if Path(f"{p}{suffix}").exists():
            raise ParkError(f"unit_layer_open:{p}{suffix}")
    started = time.monotonic()
    rec = _layer_record(p)
    size = p.stat().st_size
    part = zst.with_name(zst.name + ".part")
    part.unlink(missing_ok=True)
    meta.unlink(missing_ok=True)
    zst.unlink(missing_ok=True)   # a previous park's, its layer still whole beside it
    subprocess.run([_zstd(), "-q", f"-T{int(threads)}", f"-{int(level)}", "--check",
                    "-o", str(part), str(p)], check=True)
    _fsync(part)
    part.replace(zst)
    held = {"format": PARKED_FORMAT, "record": rec, "size": size,
            "parked_size": zst.stat().st_size,
            "parked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    tmp = meta.with_name(meta.name + ".part")
    tmp.write_text(json.dumps(held, sort_keys=True))
    _fsync(tmp)
    tmp.replace(meta)
    if p.stat().st_size != size:
        raise ParkError("unit_layer_changed_while_parking")
    p.unlink()
    return {"parked": str(zst), "size": size, "parked_size": held["parked_size"],
            "seconds": round(time.monotonic() - started, 3)}


def restore(path: str | Path, *, threads: int = 0) -> dict[str, Any]:
    """Decompress ``path.zst`` back to ``path``, checked (module docstring); a layer
    not parked: nothing done."""
    p, zst, meta = _paths(path)
    held = parked(p)
    if held is None:
        if p.is_file():
            return {"restored": "not_parked"}
        raise ParkError(f"unit_layer_missing:{p}")
    if p.exists():
        raise ParkError(f"unit_layer_both_held_and_parked:{p}")
    started = time.monotonic()
    out = p.with_name(p.name + ".restore")
    out.unlink(missing_ok=True)
    subprocess.run([_zstd(), "-q", "-d", f"-T{int(threads)}", "-o", str(out), str(zst)],
                   check=True)
    if out.stat().st_size != held["size"]:
        out.unlink()
        raise ParkError("restored_size_differs")
    if _layer_record(out) != held["record"]:
        out.unlink()
        raise ParkError("restored_record_differs")
    _fsync(out)
    out.replace(p)
    meta.unlink()
    zst.unlink()
    return {"restored": str(p), "size": held["size"],
            "seconds": round(time.monotonic() - started, 3)}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2 or args[0] not in ("park", "restore", "status"):
        print("usage: python3 -m ontology.unit_park park|restore|status LAYER", file=sys.stderr)
        return 2
    try:
        if args[0] == "park":
            got: Any = park(args[1])
        elif args[0] == "restore":
            got = restore(args[1])
        else:
            held = parked(args[1])
            got = {"parked": held is not None, "held": Path(args[1]).is_file(),
                   **({"size": held["size"], "parked_size": held["parked_size"]}
                      if held else {})}
    except (ParkError, subprocess.CalledProcessError) as exc:
        print(f"unit_park: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(got, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
