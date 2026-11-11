"""The owner's "view as <person>" (UI-07): the owner-open reader sees exactly one employee's
My work, alerts and drafts, as that employee's own screens show them, and nothing in it can
be sent, approved or logged as them.

How (2026-11-10; owner only and read only):

* The app sends ``X-PLX-View-As: <key>`` (``key_of``: an opaque digest of the employee's
  object id; no id reaches a page or a URL) with its reads while the owner views as someone.
* ``reader_for`` (the API's reader dependency) serves it only when every rule holds, else 403:
  the signed-in reader is the owner-open reader (decided from their own session, nothing in
  the request can claim it); the request is a GET or HEAD on ``ROUTES`` (My work, the
  briefing, alerts, approvals, replies, the autopilot's prepared drafts, the late list);
  the key names a person ``people`` lists (active in the Portal, cleared by the people
  exclusion, never the owner); and that person resolves again now exactly as their
  sign-in would (``session_identity.view_as_session``: an inactive, unverified or excluded
  employee is refused).
* The view-as session writes nothing: every write route refuses the header, its identity
  refuses every intent (``SessionIdentity.intent_identity``), the API logs nothing in that
  person's work log for it, and the backend neither remembers nor reads ahead for it.
* Every served or refused view-as request is audited (``Audit``): when, who viewed
  (``viewer_of``: an opaque digest of the signed-in principals, never an object id), whom
  (the key), the route and the status, appended to ``<state>/view-as/audit.jsonl`` (0600).
  A failed audit write refuses the request. Starting and stopping a view are audited too
  (``mark``: route ``view-as:start`` / ``view-as:stop``).
* The people exclusion is asked again on every request, not only when the list of
  viewable people is built, so a person excluded a moment ago can no longer be viewed.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import threading
import time
from typing import Any, Callable, Iterable, Optional

HEADER = "x-plx-view-as"
SESSION_SECONDS = 15 * 60
PEOPLE_SECONDS = 5 * 60
# The reads a view-as request may make: My work (Home, its briefing, suggestions, replies,
# action states, the late list), alerts (watching, approvals, the alert registry) and drafts
# (the autopilot's prepared items, My work's items).
ROUTES = frozenset({
    "/api/home", "/api/late", "/api/briefing", "/api/briefing/items", "/api/suggestions",
    "/api/replies", "/api/actions/state", "/api/my-work", "/api/autopilot", "/api/approvals",
    "/api/watch", "/api/alert-kinds"})
READ_ONLY = "Viewing as someone is read only: nothing can be sent, approved or logged as them."


class Refused(Exception):
    def __init__(self, message: str, status: int = 403) -> None:
        super().__init__(message)
        self.message, self.status = message, status


def key_of(oid: str) -> str:
    return hashlib.sha256(f"plx-view-as:{oid.strip().lower()}".encode()).hexdigest()[:20]


def viewer_of(reader: Any) -> str:
    """Who viewed, for the audit: a stable opaque digest of the signed-in principals (the
    same person, the same value), so the file holds no object id or address."""
    principals = ",".join(sorted(str(p).lower() for p in
                                 (getattr(getattr(reader, "session", None), "principals", ())
                                  or ())))
    return "viewer:" + hashlib.sha256(f"plx-view-as-viewer:{principals}".encode()
                                      ).hexdigest()[:20]


def name_of(email: Optional[str]) -> str:
    if email and "@" in email:
        return email.split("@", 1)[0].replace(".", " ").replace("_", " ").title()
    return "Employee"


class Audit:
    """``<state>/view-as/audit.jsonl``, appended under a lock; memory without a state dir."""

    def __init__(self, state_dir: Optional[Path]) -> None:
        self.path = Path(state_dir) / "view-as" / "audit.jsonl" if state_dir else None
        self.rows: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def record(self, *, viewer: str, key: str, route: str, status: int) -> None:
        row = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds")
               .replace("+00:00", "Z"), "viewer": viewer, "viewed": key, "route": route,
               "status": status}
        with self._lock:
            if self.path is None:
                self.rows.append(row)
                return
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, sort_keys=True) + "\n")


class ViewAs:
    """Who the owner may view as and the reader to serve them with.

    ``candidates``: (object id, address) of every active Portal user; ``identity``: the
    view-as session of one object id expiring at a time (``session_identity.view_as_session``
    over the served resolver, which refuses an excluded employee again); ``excluded``: the
    people exclusion now, as a test of one object id (it names, or cannot clear, the id);
    raising, nobody is listed. Any of them missing: nobody can be viewed (fail closed)."""

    def __init__(self, *, candidates: Optional[Callable[[], Iterable[tuple[str, str]]]] = None,
                 identity: Optional[Callable[[str, int], Any]] = None,
                 excluded: Optional[Callable[[], Callable[[str], bool]]] = None,
                 audit: Optional[Audit] = None, clock: Callable[[], float] = time.time) -> None:
        self.candidates, self.identity, self.excluded = candidates, identity, excluded
        self.audit = audit or Audit(None)
        self.clock = clock
        self._people: tuple[float, dict[str, tuple[str, str]]] | None = None
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return None not in (self.candidates, self.identity, self.excluded)

    def _table(self) -> dict[str, tuple[str, str]]:
        """key -> (object id, address) of everyone who may be viewed, kept a few minutes."""
        if not self.available:
            return {}
        now = self.clock()
        with self._lock:
            if self._people and now - self._people[0] < PEOPLE_SECONDS:
                return self._people[1]
        table: dict[str, tuple[str, str]] = {}
        try:
            rows = list(self.candidates())   # type: ignore[misc]
            test = self.excluded()   # type: ignore[misc]
        except Exception:  # noqa: BLE001 - no list or no exclusion: nobody
            rows, test = [], None

        def gone(oid: str) -> bool:
            try:
                return test is None or bool(test(oid))
            except Exception:  # noqa: BLE001 - undecidable: never viewed
                return True
        for oid, email in rows:
            oid = str(oid or "").strip().lower()
            if len(oid) != 36 or gone(oid):
                continue
            table[key_of(oid)] = (oid, str(email or "").lower())
        with self._lock:
            self._people = (now, table)
        return table

    def people(self, owner_ids: Iterable[str]) -> list[dict[str, str]]:
        mine = {str(i).lower() for i in owner_ids}
        out = [{"key": k, "name": name_of(email)} for k, (oid, email) in self._table().items()
               if oid not in mine and email not in mine]
        return sorted(out, key=lambda p: p["name"])

    def reader_for(self, owner: Any, key: str, *, method: str, route: str,
                   is_owner: Callable[[Any], bool], reader_type: Any) -> Any:
        """The reader to serve this view-as request with (rules: module doc), audited
        served or refused; ``Refused`` (403, or 404 for an unknown person)."""
        viewer = viewer_of(owner)
        key = str(key or "").strip().lower()[:40]

        def refuse(message: str, status: int = 403) -> Refused:
            try:
                self.audit.record(viewer=viewer, key=key, route=route, status=status)
            except OSError:
                pass   # refused either way
            return Refused(message, status)

        if method not in ("GET", "HEAD"):
            raise refuse(READ_ONLY)
        if not is_owner(owner):
            raise refuse("Only the owner can view as someone else.")
        if route not in ROUTES:
            raise refuse("Viewing as someone covers My work, alerts and drafts only.")
        found = self._table().get(key)
        owner_ids = {str(i).lower() for i in getattr(owner.session, "identities", ()) or ()}
        if found is None or found[0] in owner_ids or found[1] in owner_ids:
            raise refuse("No such person to view as.", 404)
        oid, email = found
        if self._excluded_now(oid):
            raise refuse("That person cannot be viewed now.", 404)
        try:
            session = self.identity(oid, int(self.clock()) + SESSION_SECONDS)  # type: ignore[misc]
        except Exception:  # noqa: BLE001 - refused now (inactive, excluded, no access)
            raise refuse("That person cannot be viewed now.", 404) from None
        if not getattr(session, "view_as", False):
            raise refuse("That person cannot be viewed now.", 404)
        try:
            self.audit.record(viewer=viewer, key=key, route=route, status=200)
        except OSError:
            raise Refused("The view could not be audited, so it is not shown.", 503) from None
        return reader_type(session, getattr(owner, "mode", "entra"), name_of(email), email)

    def _excluded_now(self, oid: str) -> bool:
        """The people exclusion asked now (not the cached list): excluded, or undecidable."""
        try:
            test = self.excluded()   # type: ignore[misc]
            return test is None or bool(test(oid))
        except Exception:  # noqa: BLE001 - undecidable: never viewed
            return True

    def mark(self, owner: Any, key: str, event: str, *, is_owner: Callable[[Any], bool]
             ) -> None:
        """Audit the owner starting or stopping a view (``event``: start or stop).
        ``Refused``: not the owner (403), starting on someone who cannot be viewed (404),
        the audit not written (503). Stopping needs no viewable person: it is always
        recorded for the owner."""
        viewer, key = viewer_of(owner), str(key or "").strip().lower()[:40]
        route = f"view-as:{event}"

        def refuse(message: str, status: int) -> Refused:
            try:
                self.audit.record(viewer=viewer, key=key, route=route, status=status)
            except OSError:
                pass
            return Refused(message, status)

        if event not in ("start", "stop"):
            raise refuse("No such view-as event.", 404)
        if not is_owner(owner):
            raise refuse("Only the owner can view as someone else.", 403)
        if event == "start":
            found = self._table().get(key)
            if found is None or self._excluded_now(found[0]):
                raise refuse("No such person to view as.", 404)
        try:
            self.audit.record(viewer=viewer, key=key, route=route, status=200)
        except OSError:
            raise Refused("The view could not be audited.", 503) from None
