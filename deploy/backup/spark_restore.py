"""The restore test's throwaway restore on a Spark (11-23: the box never has the ~100 GB the
restored copy needs, so its own restore test was skipped by its disk guard every night and
nothing proved the dump restores).

``restore_on(host, dump, want)``: this box streams the dump over ssh (the same tailnet ssh
config the off-box copy uses) into ``pg_restore`` run on the Spark against that Spark's
throwaway Postgres (``~/plx-tests/pg``, socket only; the test runner's), into a database named
for this run that is ALWAYS dropped. The plaintext dump is never written to the Spark's disk
(only the restored database is, and it is dropped). Row counts of every table the manifest
counted are then read on the Spark and returned for the caller's comparison with the manifest.
Never touches the live database. Any ssh failure raises ``SparkRestoreError`` (the test then
fails: a restore that did not run is never a pass).
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

SSH_CONFIG = "/data/plx-state/ts-petra/ssh_config"
PG_HOME = "$HOME/plx-tests"               # the Postgres binaries and the disk the restore is checked on
# the restore's OWN cluster (11-23: it shared the test runner's cluster, which sits at its
# connection limit while a test batch runs: "too many clients", psql exit 2, the restore test failed)
RESTORE_HOME = "$HOME/plx-restore"
RESTORE_PORT = 5434
MIN_FREE_GB = 160
_NAME = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$")
_DB = re.compile(r"^[a-z][a-z0-9_]{0,40}$")

Runner = Callable[..., subprocess.CompletedProcess]


class _Text:
    """A stdin for ``subprocess.run`` holding text (a real file object for a runner that
    wants ``fileno``)."""
    def __new__(cls, text: str):
        import tempfile
        f = tempfile.TemporaryFile()
        f.write(text.encode())
        f.seek(0)
        return f


class SparkRestoreError(RuntimeError):
    pass


def _env_prefix() -> str:
    return (f'export PATH={PG_HOME}/pg/bin:$PATH PGHOST={RESTORE_HOME}/run PGPORT={RESTORE_PORT}; ')


# make the restore's own cluster if it is missing (socket only, few connections) and start it
START = (f"mkdir -p {RESTORE_HOME}/run; [ -f {RESTORE_HOME}/data/PG_VERSION ] || "
         f"initdb -D {RESTORE_HOME}/data -A trust >/dev/null; "
         f"pg_ctl -D {RESTORE_HOME}/data status >/dev/null 2>&1 || "
         f"pg_ctl -D {RESTORE_HOME}/data -l {RESTORE_HOME}/pg.log -w -o "
         f"'-p {RESTORE_PORT} -k {RESTORE_HOME}/run -c listen_addresses= -c max_connections=20' start >/dev/null; ")


def _ssh(host: str, script: str, *, stdin: Any = None, runner: Runner = subprocess.run,
         timeout: int = 7200) -> subprocess.CompletedProcess:
    cfg = os.environ.get("PLX_OFFBOX_SSH_CONFIG", SSH_CONFIG)
    done = runner(["ssh", "-F", cfg, host, "bash -c " + shlex.quote(_env_prefix() + script)],
                  stdin=stdin if stdin is not None else subprocess.DEVNULL,
                  capture_output=True, text=True, timeout=timeout)
    if done.returncode != 0:
        # the step, the exit code and the FIRST line of pg_restore's own error (an object or
        # an extension name, 100 characters; never data rows: only lines naming ``error:``)
        first = next((ln for ln in (done.stderr or "").splitlines()
                      if "error:" in ln and "COPY" not in ln), "")
        raise SparkRestoreError(f"{script.split()[0]} exited {done.returncode}"
                                + (f": {first[:100]}" if first else ""))
    return done


_EXT = re.compile(r"^\d+; \d+ \d+ EXTENSION - (\S+)|^\d+; \d+ \d+ COMMENT - EXTENSION (\S+)")


def without_unavailable_extensions(toc: str, available: Sequence[str]) -> tuple[str, list[str]]:
    """The dump's table of contents (``pg_restore -l``) minus the EXTENSION entries (and their
    comments) this Spark's Postgres does not carry (pg_repack and the like are boxes' tools, not
    data): the list for ``pg_restore -L`` and the extension names left out."""
    keep, left = [], []
    for line in toc.splitlines():
        m = _EXT.match(line)
        name = (m.group(1) or m.group(2)) if m else None
        if name and name not in available and name != "plpgsql":
            left.append(name)
            continue
        keep.append(line)
    return "\n".join(keep) + "\n", sorted(set(left))


def restore_on(host: str, dump: Path, want: Mapping[str, Any], db: str, *, toc: str = "",
               runner: Runner = subprocess.run) -> dict[str, int]:
    """Restore ``dump`` into throwaway database ``db`` on ``host`` and return its row counts
    for the tables in ``want`` whose manifest count is not None."""
    if not _DB.match(db) or not re.match(r"^[A-Za-z0-9._-]+$", host):
        raise SparkRestoreError("bad name")
    tables = sorted(t for t, n in want.items() if n is not None)
    if not all(_NAME.match(t) for t in tables):
        raise SparkRestoreError("unexpected table name in the manifest")
    free = _ssh(host, f"df -BG --output=avail {PG_HOME} | tail -1", runner=runner).stdout
    if int(re.sub(r"\D", "", free) or 0) < MIN_FREE_GB:
        raise SparkRestoreError(f"disk_guard:{free.strip()}<{MIN_FREE_GB}G")
    _ssh(host, f"{START}dropdb --if-exists --force {db}; createdb {db}", runner=runner)
    use = ""
    if toc:
        have = _ssh(host, "psql -X -At -d postgres -c 'SELECT name FROM pg_available_extensions'",
                    runner=runner).stdout.split()
        listing, _left = without_unavailable_extensions(toc, have)
        _ssh(host, f"cat > $HOME/{db}.list", stdin=_Text(listing), runner=runner)
        use = f"-L $HOME/{db}.list "
    try:
        with open(dump, "rb") as f:
            _ssh(host, f"pg_restore --exit-on-error --no-owner --no-privileges {use}-d {db}",
                 stdin=f, runner=runner)
        sql = " UNION ALL ".join(
            f"SELECT '{t}', count(*) FROM {t.split('.')[0]}.{t.split('.')[1]}" for t in tables)
        out = _ssh(host, f"psql -X -At -F '|' -d {db} -c {shlex.quote(sql)}", runner=runner)
        counts = {}
        for line in out.stdout.splitlines():
            name, _, n = line.partition("|")
            counts[name] = int(n)
        return counts
    finally:
        try:
            _ssh(host, f"dropdb --if-exists --force {db}; rm -f $HOME/{db}.list; "
                       f"pg_ctl -D {RESTORE_HOME}/data -m fast stop >/dev/null 2>&1; true",
                 runner=runner, timeout=600)
        except Exception:  # noqa: BLE001 - the next run drops a stale one first
            pass
