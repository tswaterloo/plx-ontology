"""The projection a live case opens on: host refusals, capture gaps, the served-generation
pin and the projection run (``operating.case_workspace_live``, which re-exports them)."""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Callable, Mapping, Sequence

from .case_workspace_live_model import INDEX_DIR, OpenError


def ingested(state_dir: str | Path) -> bool:
    """True when the ingestion runner has written this state directory."""
    from .ingestion import STORE_DIR, STORE_FILE

    return (Path(state_dir) / STORE_DIR / STORE_FILE).is_file()


def _read_only(path: Path, query: str) -> list[tuple] | None:
    try:
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            return db.execute(query).fetchall()
        finally:
            db.close()
    except sqlite3.Error:
        return None


_PART_TEXT = re.compile(r"[A-Za-z0-9_.:-]{1,79}")
_PART_KEYS = ("part", "source", "error_code", "cause", "permission")


def host_refusals(state_dir: str | Path) -> dict[str, list[dict[str, Any]]]:
    """Per projection source (``host.PROJECTED_SOURCE``) of each source other than the
    Portal (``portal_upstream``) the host configures, the ``blocked_upstream`` parts its
    latest read was refused or not answered on, read-only from the host ledger
    (``host.blocked_parts``); none when no host ran. Each part is rebuilt from its names,
    status and codes only; a row that does not read keeps its source blocked
    (``host_ledger_unreadable``), never cleared."""
    from .host import _UPSTREAM, HOST_DIR, LEDGER_FILE, PROJECTED_SOURCE

    host = Path(state_dir) / HOST_DIR / LEDGER_FILE
    rows = (_read_only(host, (
        "SELECT b.source, b.parts FROM source_blocked b JOIN source_state s ON s.source = "
        "b.source WHERE s.configured = 1 AND b.source != 'portal' ORDER BY b.source"))
            or []) if host.is_file() else []
    refused: dict[str, list[dict[str, Any]]] = {}
    for source, text in rows:
        if source not in PROJECTED_SOURCE:
            continue
        try:
            listed = json.loads(text)
        except (TypeError, ValueError):
            listed = None
        parts = []
        for part in listed if isinstance(listed, list) else [None]:
            entry: dict[str, Any] = {key: part[key] for key in _PART_KEYS
                                     if isinstance(part, Mapping) and isinstance(
                                         part.get(key), str)
                                     and _PART_TEXT.fullmatch(part[key])}
            status = part.get("http_status") if isinstance(part, Mapping) else None
            if isinstance(status, int) and not isinstance(status, bool):
                entry["http_status"] = status
            if "part" not in entry or "error_code" not in entry:
                entry = {"part": source, "error_code": "host_ledger_unreadable"}
            parts.append({"source": _UPSTREAM[source], **entry})
        refused[PROJECTED_SOURCE[source]] = parts
    return refused


def host_capture_gaps(state_dir: str | Path) -> dict[str, dict[str, Any]]:
    """Per projection source (``host.PROJECTED_SOURCE``) the host configures and whose
    latest capture its own budget cut (``source_state.complete = 0`` and the latest cycle's
    outcome for it partial with a ``*_budget`` reason: ``time_budget``, ``page_budget``),
    the typed ``capture_gap`` part a case that needed that source is blocked on, read-only
    from the host ledger; none when no host ran. A source that finished, was never
    configured, has no row or is incomplete for another reason (a unit that failed) is no
    budget gap."""
    from .host import _UPSTREAM, HOST_DIR, LEDGER_FILE, PROJECTED_SOURCE

    host = Path(state_dir) / HOST_DIR / LEDGER_FILE
    if not host.is_file():
        return {}
    cut = {source for (source,) in _read_only(host, (
        "SELECT source FROM source_state WHERE configured = 1 AND complete = 0")) or ()}
    latest = _read_only(host, "SELECT report FROM cycle ORDER BY cycle_id DESC LIMIT 1")
    try:
        sources = json.loads(latest[0][0]).get("sources") if latest else None
    except (TypeError, ValueError, AttributeError):
        sources = None
    gaps = {}
    for source in sorted(cut):
        outcome = sources.get(source) if isinstance(sources, dict) else None
        reasons = outcome.get("reasons") if isinstance(outcome, dict) else None
        if source in PROJECTED_SOURCE and source != "portal" \
                and isinstance(outcome, dict) and outcome.get("state") == "partial" \
                and any(isinstance(r, str) and r.endswith("_budget")
                        for r in reasons or ()):
            gaps[PROJECTED_SOURCE[source]] = {
                "part": source, "source": _UPSTREAM[source],
                "error_code": "capture_budget_cut", "cause": "capture_gap"}
    return gaps


def upstream_parts(reasons: Sequence[str] | None) -> list[dict[str, str]]:
    """``portal_upstream`` reasons as the live verifier's ``blocked_upstream`` parts
    (the live verifier: the check is then blocked, never a pass)."""
    parts = {("host:portal" if reason.startswith("host_error:") else
              "ingestion:portal_relation", reason.split(":", 1)[1][:79])
             for reason in reasons or ()}
    return [{"part": part, "source": "portal-workbench-api", "error_code": code}
            for part, code in sorted(parts)]


def projection_file(state_dir: str | Path, tenant: str) -> Path:
    """Where the case workspace keeps ``tenant``'s projection of the state directory: its
    content (reviewed links, confirmations) is the tenant's, so each tenant has its own
    file, named by a digest of the tenant (never a path the tenant's text could shape)."""
    from hashlib import sha256

    digest = sha256(tenant.encode("utf-8")).hexdigest()[:16]
    return Path(state_dir) / INDEX_DIR / f"ontology-{digest}.sqlite"


def project_state(state_dir: str | Path, path: str | Path, *, tenant: str,
                  internal_domains: Sequence[str] = (),
                  checkpoint: Callable[[], None] | None = None) -> dict[str, Any]:
    """Project every capture the state directory holds (the Portal mirror; BC, mail and
    SharePoint captures) into ``path``, offline: no source system is contacted. The new
    store replaces the old one by rename, so a reader never opens a half-built file.

    The build runs the host's ``post_project`` steps (``operating.build_steps``) over the
    state directory's review and confirmation stores for ``tenant``: a link a person
    accepted is ``reviewed`` and one they rejected ``contradicted``
    (``ontology.reviews.link_step``), so a live Portal<->BC customer ``same_record_as``
    link, a lead until reviewed, joins a case pick's scope only once a person accepted it,
    and a confirmed document kind reaches its SharePoint file as the host projects it. The
    stores are read if present, never created or written: the ``confirmations`` step runs
    over a copy made beside ``path`` (``post_project(scratch=)``: the document kinds the
    review store holds are imported into the copy, as the host's next build imports them).
    A review store that is there but unreadable fails the build
    (``ReviewStoreUnreadable``), never a build that would serve a rejected link as a lead."""
    from contextlib import ExitStack

    from ontology import project
    from ontology.schema import load_schema

    from . import build_steps
    from .case_runtime import CASE_RUNTIME_DIR, MIRROR_FILE
    from .host import state_captures

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.build")
    temp.unlink(missing_ok=True)
    with ExitStack() as stack:
        sources = state_captures(Path(state_dir), stack, internal_domains=internal_domains)
        mirror = Path(state_dir) / CASE_RUNTIME_DIR / MIRROR_FILE
        if mirror.is_file():
            captures = stack.enter_context(project.MirrorCaptures(mirror))
            sources[captures.source] = captures
        steps = build_steps.post_project(state_dir, tenant, scratch=path.parent)
        try:
            report = project.build_store(temp, load_schema(), sources, post_project=steps,
                                         checkpoint=checkpoint)
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
    os.replace(temp, path)
    return report


# ------------------------------------------------------------------ the run's projection
#
# A state directory a persistent host serves (``operating.host``: ``<DIR>/projection``,
# generations committed by ``ontology.project_incremental``) already holds the projection
# of every capture: a run reads that generation, read-only, and never builds its own copy
# (a cold private build of a full state directory needs as much disk again as the served
# one). A private build happens only when the directory has no served generation, or when
# the served one cannot be shown to carry this tenant's decisions, and only with room.

SERVED_DIR = "projection"  # ``operating.host.PROJECTION_DIR`` (not imported: host is heavy)
PIN_PREFIX = ".served-"
# A private build starts only with this much free beyond its estimate (the host's own
# floor, ``operating.host.MIN_FREE_BYTES``), and stops when free space falls below it.
PRIVATE_FREE_FLOOR = 4 * 1024 ** 3


class ProjectionDiskLow(OpenError):
    """A private projection build refused or stopped: too little free disk."""

    def __init__(self, free: int | None, needed: int) -> None:
        super().__init__("projection_disk_low")
        self.free, self.needed = free, needed


class RunProjection:
    """The projection one run reads: ``path``, ``source`` (``served``: the host's
    committed generation; ``private``: built for this run), ``reason`` (why that one) and,
    when served, the ``generation`` and ``content_digest``. A served generation is
    pinned (``pinned``): a hard link to its store in ``case-workspace-live`` keeps the
    whole generation readable for the run while the host commits and prunes newer ones;
    :meth:`close` drops the link. A committed generation is never written again, so every
    read of the run sees one consistent projection."""

    def __init__(self, path: Path, source: str, reason: str, *,
                 generation: str | None = None, content_digest: str | None = None,
                 pin: Path | None = None) -> None:
        self.path, self.source, self.reason = Path(path), source, reason
        self.generation, self.content_digest, self.pin = generation, content_digest, pin

    @property
    def pinned(self) -> bool:
        return self.pin is not None

    def summary(self) -> dict[str, Any]:
        """Counts and enums only (no path)."""
        return {"source": self.source, "reason": self.reason,
                "generation": self.generation, "pinned": self.pinned}

    def close(self) -> None:
        if self.pin is not None:
            self.pin.unlink(missing_ok=True)
            self.pin = None


def _foreign_or_unproven(state_dir: Path, tenant: str, generation: Any) -> str | None:
    """Why the served ``generation`` cannot stand for ``tenant``'s projection, or None.
    The host records no tenant with a generation; its ``post_project`` rows are its own
    tenant's decisions as they were when the build started. So it is reused only when the
    decision stores hold no other tenant's rows; when ``tenant`` has decisions, the build
    read some (the host's tenant is then ``tenant``) and started after the newest of them.
    A store or time that cannot be read is unproven."""
    from ontology import reviews
    from ontology.store import instant

    from . import kind_confirmation

    own, newest = 0, []
    for path, table, at in (
            (reviews.store_path(state_dir), "reviews", "recorded_at"),
            (kind_confirmation.store_path(state_dir), "confirmation",
             "json_extract(record_json, '$.confirmed_at')")):
        if not path.is_file():
            continue
        try:
            with contextlib.closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro",
                                                    uri=True, timeout=5)) as db:
                if db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND "
                              "name = ?", (table,)).fetchone() is None:
                    continue
                other, mine = db.execute(
                    f"SELECT COALESCE(SUM(tenant != ?), 0), COALESCE(SUM(tenant = ?), 0) "
                    f"FROM {table}", (tenant, tenant)).fetchone()
                if mine:
                    newest.append(db.execute(f"SELECT MAX({at}) FROM {table} "
                                             "WHERE tenant = ?", (tenant,)).fetchone()[0])
        except sqlite3.Error:
            return "decision_store_unreadable"
        if other:
            return "served_tenant_unproven"
        own += mine
    if not own:
        return None
    steps = generation.report.get("post_project") or {}
    read = sum(int(((steps.get(name) or {}).get("counts") or {}).get(key) or 0)
               for name, key in (("reviews", "decisions"), ("confirmations", "confirmations")))
    if not read:
        return "served_tenant_unproven"
    try:
        with contextlib.closing(sqlite3.connect(
                generation.store.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            built = instant(db.execute("SELECT value FROM store_meta WHERE key = 'built_at'"
                                       ).fetchone()[0])
        if any(instant(at) >= built for at in newest):
            return "served_older_than_decisions"
    except (sqlite3.Error, TypeError, ValueError):
        return "served_tenant_unproven"
    return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _sweep_pins(directory: Path) -> None:
    """Drop the pins of runs no longer running (a killed run leaves its hard link)."""
    for pin in directory.glob(f"{PIN_PREFIX}*.sqlite"):
        match = re.fullmatch(re.escape(PIN_PREFIX) + r"[0-9A-Za-z]+-(\d+)-[0-9a-f]+\.sqlite",
                             pin.name)
        if match and not _pid_alive(int(match.group(1))):
            pin.unlink(missing_ok=True)


def _pin(state_dir: Path, generation: Any) -> Path | None:
    """A hard link to ``generation``'s store in ``case-workspace-live`` (no bytes copied),
    checked to be that generation (its content digest); None when the file system cannot
    link it (another device, no permission) or the generation was pruned meanwhile."""
    import secrets

    directory = state_dir / INDEX_DIR
    directory.mkdir(parents=True, exist_ok=True)
    _sweep_pins(directory)
    pin = directory / f"{PIN_PREFIX}{generation.id}-{os.getpid()}-{secrets.token_hex(4)}.sqlite"
    try:
        os.link(generation.store, pin)
    except OSError:
        return None
    try:
        with contextlib.closing(sqlite3.connect(pin.resolve().as_uri() + "?mode=ro",
                                                uri=True)) as db:
            row = db.execute("SELECT value FROM store_meta WHERE key = 'content_digest'"
                             ).fetchone()
    except sqlite3.Error:
        row = None
    if row is None or row[0] != generation.content_digest:
        pin.unlink(missing_ok=True)
        return None
    return pin


def served_generation(state_dir: str | Path, tenant: str) -> tuple[Any, str]:
    """(the host's committed generation this run may read for ``tenant``, or None; the
    reason code). Read-only: ``CURRENT`` and the generation's store are only read."""
    from ontology.project_incremental import current_generation

    generation = current_generation(Path(state_dir) / SERVED_DIR)
    if generation is None:
        return None, "no_served_generation"
    refused = _foreign_or_unproven(Path(state_dir), tenant, generation)
    if refused:
        return None, refused
    return generation, "served_generation"


def run_projection(state_dir: str | Path, tenant: str, *,
                   internal_domains: Sequence[str] = (),
                   disk_free: Callable[[Path], int] | None = None) -> RunProjection:
    """The projection a run reads (:class:`RunProjection`; the caller closes it). The
    host's served generation when there is one this tenant may use (opened read-only,
    pinned; ``internal_domains`` is then the host's own mail scope); else a private build
    at :func:`projection_file` (:func:`project_state`), refused before it starts when free
    space is below ``PRIVATE_FREE_FLOOR`` plus the last private file's size, and stopped
    (:class:`ProjectionDiskLow`, partial files removed) when free space falls below the
    floor while it runs."""
    import shutil

    state = Path(state_dir)
    free_of = disk_free or (lambda path: shutil.disk_usage(path).free)
    generation, reason = served_generation(state, tenant)
    for _attempt in range(2):   # a generation pruned between reading CURRENT and linking
        if generation is None:
            break
        pin = _pin(state, generation)
        if pin is not None:
            return RunProjection(pin, "served", reason, generation=generation.id,
                                 content_digest=generation.content_digest, pin=pin)
        again, why = served_generation(state, tenant)
        if again is not None and again.id == generation.id:
            # Linking is not possible here: read the generation in place, unpinned (the
            # host keeps the previous generation on disk after one commit, ``KEEP``).
            return RunProjection(generation.store, "served", "served_generation_unpinned",
                                 generation=generation.id,
                                 content_digest=generation.content_digest)
        generation, reason = again, why
    path = projection_file(state, tenant)
    path.parent.mkdir(parents=True, exist_ok=True)
    previous = path.stat().st_size if path.is_file() else 0

    def free() -> int | None:
        try:
            return int(free_of(path.parent))
        except OSError:
            return None

    needed = PRIVATE_FREE_FLOOR + previous
    before = free()
    if before is None or before < needed:
        raise ProjectionDiskLow(before, needed)

    def checkpoint() -> None:
        now = free()
        if now is None or now < PRIVATE_FREE_FLOOR:
            raise ProjectionDiskLow(now, PRIVATE_FREE_FLOOR)

    project_state(state, path, tenant=tenant, internal_domains=internal_domains,
                  checkpoint=checkpoint)
    return RunProjection(path, "private", reason)


_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9#._/-]*")


def _value_texts(value: Any) -> list[str]:
    """Comparable texts of one row value (an integral number also as an integer)."""
    if value is None or isinstance(value, bool):
        return []
    if isinstance(value, float) and value.is_integer():
        return [str(int(value)), str(value)]
    if isinstance(value, (int, float)):
        return [str(value)]
    if isinstance(value, str):
        text = value.strip()
        if re.fullmatch(r"-?\d+\.0+", text):
            return [text, text.split(".", 1)[0]]
        return [text] if text else []
    return []


def _row_texts(node: Any, out: set[str]) -> None:
    stack = [node]
    while stack:
        item = stack.pop()
        if isinstance(item, Mapping):
            stack.extend(item.values())
        elif isinstance(item, (list, tuple)):
            stack.extend(item)
        else:
            out.update(_value_texts(item))


def _plain(value: Any) -> Any:
    """A tool value without its untrusted-text envelope (the source text itself)."""
    if isinstance(value, Mapping) and isinstance(value.get("untrusted_text"), str):
        return value["untrusted_text"]
    return value


def _mirror_relation(handle: Any) -> tuple[str, dict[str, Any]] | None:
    from integrations.source_mirror import parse_citation

    try:
        relation, _canonical, key, _capture = parse_citation(handle)
    except ValueError:
        return None
    return relation, key


def cited_rows(result: Any) -> list[tuple[str, set[str]]]:
    """(relation, value texts) for every mirror-cited value in a tool result: a value
    with its ``citation``, or source text whose envelope names its ``origin`` row. The
    row's key values count as its texts too (an object id carries them)."""
    out: list[tuple[str, set[str]]] = []
    stack = [result]
    while stack:
        item = stack.pop()
        if isinstance(item, Mapping):
            cite = item.get("citation")
            handle = cite.get("l1_handle") if isinstance(cite, Mapping) else None
            value = item.get("value")
            if handle is None and isinstance(item.get("untrusted_text"), str):
                handle = (item.get("origin") or {}).get("citation")
                value = item["untrusted_text"]
            found = _mirror_relation(handle) if isinstance(handle, str) else None
            if found is not None:
                texts: set[str] = set()
                _row_texts([_plain(value), found[1]], texts)
                out.append((found[0], texts))
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return out


def case_tokens(texts: Sequence[Any]) -> list[str]:
    """Identifier-shaped tokens (a digit, 3+ characters) of the case's own words."""
    out: list[str] = []
    for text in texts:
        for token in _TOKEN.findall(str(text or "")):
            token = token.rstrip("._/-")
            if len(token) >= 3 and any(ch.isdigit() for ch in token) and token not in out:
                out.append(token)
    return out


class _StampedCases:
    """A case store view whose ``open`` records ``kind`` as the question's author.

    The in-process MCP server stamps every ``plx.case_open`` question ``agent`` (an
    agent supplied it). When a person's session opens the case (``SessionIdentity``
    with ``author_kind == "human"``, the console ``--open``), the question is theirs:
    this view replaces the stamp with the session's kind, and ``CaseStore.open``
    still refuses ``human`` for an actor holding an agent principal."""

    def __init__(self, cases: Any, kind: str) -> None:
        self._cases, self._kind = cases, kind

    def open(self, *args: Any, **kwargs: Any) -> str:
        kwargs["objective_author_kind"] = self._kind
        return self._cases.open(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cases, name)


class _StampedRuntime:
    def __init__(self, runtime: Any, kind: str) -> None:
        self._runtime = runtime
        self.cases = _StampedCases(runtime.cases, kind)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._runtime, name)
