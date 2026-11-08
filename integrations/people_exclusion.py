"""People exclusion: the one guard mail, Teams chats and OneDrive share.

Some people's mail, chats and OneDrives are never read, stored or indexed. They are
named by Entra object ID (later_ask ``excluded-people-oids``), never by name, and refused
before any request that would read them: a mailbox open, a chat-member lookup, a
chat's messages, a OneDrive's libraries. A message an excluded person sent, or that is
addressed or copied to one (mail), is dropped before its body is read or stored.

The ID list comes from the environment (``PLX_EXCLUDED_PEOPLE_OIDS``: object IDs
separated by commas or whitespace) and/or a JSON file outside the git tree
(``PLX_EXCLUDED_PEOPLE_FILE``, same shape as the committed ``people_exclusion.json``,
whose ``object_ids`` stays empty: IDs are never committed). With no list, an empty
list or any invalid entry the guard is unavailable and every read it gates fails
closed with ``ExclusionUnavailable`` (a typed block, never a pass).

Addresses: a scope names mailboxes and chat members by address, but the guard is keyed
by object ID. ``bind(api)`` reads the whole tenant directory once (``GET /users``, a
listing that names nobody) and learns the excluded people's addresses, UPNs and
OneDrive paths. A failed or truncated listing leaves the guard unresolved, so every
address-named read fails closed too.

Offline (no listing this process): ``with_directory`` attaches what the mail capture
recorded, read-only. The captured mailboxes' own identities map their addresses to object
ids; the tenant listing the capture's last bound guard recorded (``tenant_listing_rows``,
``mail_capture_listing``) adds the addresses of every listed user the exclusion did not
name (each with its object id) and of every group the directory lists. An address is
decided only by membership there: a user's by that user's object id (excluded if the list
names it now), a group's as no person's; any other address (an alias, a plus-address, an
address of a deleted or excluded user, a display name) stays undecided (withheld). Nothing
about an excluded person is recorded, and a listing older than ``DIRECTORY_MAX_AGE``
decides nothing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
import re
import stat
import subprocess
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import live_source_grammar as grammar
from .live_source_guard import NotVisible

SCHEMA = "plx-people-exclusion/v1"
ENV_IDS = "PLX_EXCLUDED_PEOPLE_OIDS"
ENV_FILE = "PLX_EXCLUDED_PEOPLE_FILE"
CONFIG = Path(__file__).with_name("people_exclusion.json")
LATER_ASK = "excluded-people-oids"

# Refusal kinds: what the refused read named.
KINDS = frozenset({"mailbox", "chat_member", "chat", "sender", "recipient",
                   "onedrive_owner"})
# Why the guard is unavailable.
CODES = frozenset({"excluded_people_oids_absent", "excluded_people_oids_invalid",
                   "excluded_people_file_unreadable", "excluded_people_file_in_git",
                   "excluded_people_unresolved"})

# The unattended passes' env file (deploy/systemd/plx-proactive*.service EnvironmentFile):
# a process started outside those units (an operator's or a harness's run of the engine)
# reads the exclusion from it when its own environment names none.
ENV_FILE_SETTING = "PLX_EXCLUSION_ENV_FILE"
DEFAULT_ENV_FILE = "~/.config/plx/proactive.env"
_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def environ_with_exclusion(environ: Mapping[str, str] | None = None, *,
                           env_file: str | Path | None = None) -> Mapping[str, str]:
    """``environ`` (default ``os.environ``) plus the ``PLX_EXCLUDED_PEOPLE_*`` entries of
    the passes' env file when ``environ`` names neither ``ENV_IDS`` nor ``ENV_FILE``.

    The file is ``env_file``, else ``PLX_EXCLUSION_ENV_FILE`` (empty: none), else
    ``DEFAULT_ENV_FILE``. It is read only when it is a regular file owned by this user,
    readable by nobody else and outside git; only ``PLX_EXCLUDED_PEOPLE_*`` lines are
    taken (systemd ``EnvironmentFile`` syntax: ``KEY=value``, optional quotes, ``#``
    comments). Anything else leaves ``environ`` as it is, so the guard built from it stays
    unavailable (fail closed); nothing is printed or returned but the merged mapping."""
    environ = os.environ if environ is None else environ
    if environ.get(ENV_IDS) is not None or environ.get(ENV_FILE):
        return environ
    named = env_file if env_file is not None else environ.get(ENV_FILE_SETTING,
                                                              DEFAULT_ENV_FILE)
    if not named:
        return environ
    path = Path(named).expanduser()
    try:
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() \
                or info.st_mode & 0o077 or _inside_git(path.resolve()):
            return environ
        text = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return environ
    found: dict[str, str] = {}
    for line in text.splitlines():
        match = _ENV_LINE.match(line)
        if match is None or line.lstrip().startswith("#"):
            continue
        key, value = match.groups()
        if not key.startswith("PLX_EXCLUDED_PEOPLE_"):
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        found[key] = value
    return {**environ, **found} if found else environ


_GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                   r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_DIRECTORY_SELECT = "id,userPrincipalName,mail,proxyAddresses"


class ExclusionUnavailable(NotVisible):
    """The guard cannot decide, so the read it gates is refused (fail closed). A
    ``NotVisible``: a caller that knows nothing of the guard still withholds."""

    def __init__(self, code: str) -> None:
        if code not in CODES:
            raise ValueError("unknown exclusion code")
        super().__init__(code)


class PeopleExcluded(NotVisible):
    """A read named an excluded person; refused before any request for it."""

    def __init__(self, kind: str) -> None:
        if kind not in KINDS:
            raise ValueError("unknown exclusion kind")
        super().__init__("excluded_person")
        self.kind = kind


def is_object_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_GUID.match(value))


def onedrive_path(upn: str) -> str:
    """The personal-site path segment Microsoft derives from a UPN (``.``, ``@`` → ``_``)."""
    return re.sub(r"[.@]", "_", upn.lower())


def _inside_git(path: Path) -> bool:
    try:
        probe = subprocess.run(["git", "-C", str(path.parent), "rev-parse", "--is-inside-work-tree"],
                               capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0 and probe.stdout.strip() == "true"


def _ids_from(values: Iterable[Any]) -> tuple[frozenset[str] | None, bool]:
    """(the lower-cased IDs, valid); one invalid entry invalidates the whole list."""
    found = set()
    for value in values:
        if not is_object_id(value):
            return None, False
        found.add(value.lower())
    return frozenset(found), True


def _config_ids(path: Path) -> list[Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, Mapping) or data.get("schema") != SCHEMA \
            or not isinstance(data.get("object_ids"), list):
        raise ValueError("bad exclusion config")
    return list(data["object_ids"])


class PeopleExclusion:
    """The excluded object IDs plus, once bound, their addresses and OneDrive paths."""

    def __init__(self, object_ids: Iterable[str] | None = None, *,
                 unavailable: str | None = None) -> None:
        self._unavailable = unavailable
        ids: frozenset[str] = frozenset()
        if unavailable is None:
            parsed, valid = _ids_from(list(object_ids or ()))
            if not valid:
                self._unavailable = "excluded_people_oids_invalid"
            elif not parsed:
                self._unavailable = "excluded_people_oids_absent"
            else:
                ids = parsed
        elif unavailable not in CODES:
            raise ValueError("unknown exclusion code")
        self._ids = ids
        self._addresses: frozenset[str] = frozenset()
        self._onedrive: frozenset[str] = frozenset()
        self._bound = False
        self._bind_failed = False
        # A captured directory (``attach_directory``): address -> object id and OneDrive
        # path -> object id of every user it lists; None: not loaded.
        self._directory: dict[str, str] | None = None
        self._directory_onedrive: dict[str, str] = {}
        # The last complete directory listing ``bind`` read: (oid, UPN, SMTP addresses) of
        # every user, and when; None until bound (``tenant_listing_rows`` records it).
        self._listing: list[tuple[str, str | None, list[str]]] | None = None
        self._listing_at: datetime | None = None
        # The group addresses of the last complete groups listing (``list_groups``);
        # None: not read.
        self._groups: frozenset[str] | None = None
        # A recorded tenant listing (``attach_listing``): address -> object id of each
        # cleared listed user, and the group addresses; None: not loaded.
        self._tenant: dict[str, str] | None = None
        self._tenant_groups: frozenset[str] = frozenset()
        self.dropped = 0

    def __repr__(self) -> str:
        state = self._unavailable or ("bound" if self._bound else "ids_only")
        return f"PeopleExclusion({state}, {len(self._ids)} ids)"

    @classmethod
    def from_env(cls, environ: Mapping[str, str], *, config: Path = CONFIG) -> "PeopleExclusion":
        """IDs from ``PLX_EXCLUDED_PEOPLE_OIDS``, ``PLX_EXCLUDED_PEOPLE_FILE`` (outside
        git) and the committed config, merged. Any unreadable source or bad entry makes
        the guard unavailable; none at all is ``excluded_people_oids_absent``."""
        values: list[Any] = []
        raw = environ.get(ENV_IDS)
        if raw is not None:
            values.extend(v for v in re.split(r"[\s,;\"']+", raw) if v)
        sources = [config]
        named = environ.get(ENV_FILE)
        if named:
            path = Path(named).expanduser()
            if not path.is_absolute() or not path.is_file():
                return cls(unavailable="excluded_people_file_unreadable")
            if _inside_git(path.resolve()):
                return cls(unavailable="excluded_people_file_in_git")
            sources.append(path)
        for source in sources:
            try:
                if source == config and not source.is_file():
                    continue
                values.extend(_config_ids(source))
            except (OSError, ValueError):
                return cls(unavailable="excluded_people_file_unreadable")
        return cls(values)

    # ---------------------------------------------------------------- state

    @property
    def unavailable(self) -> str | None:
        """The ``CODES`` entry that makes the guard refuse everything, or None."""
        if self._unavailable:
            return self._unavailable
        return "excluded_people_unresolved" if self._bind_failed else None

    @property
    def bound(self) -> bool:
        return self._bound

    @property
    def count(self) -> int:
        return len(self._ids)

    def require(self) -> None:
        """Raise ``ExclusionUnavailable`` unless the ID list is usable (no I/O)."""
        code = self.unavailable
        if code:
            raise ExclusionUnavailable(code)

    def require_bound(self) -> None:
        self.require()
        if not self._bound:
            raise ExclusionUnavailable("excluded_people_unresolved")

    def bind(self, api: Any) -> None:
        """Learn the excluded people's addresses and OneDrive paths from one complete
        directory listing (``GET /users``, every page). The listing names nobody, so
        it is not a read of any excluded person. A failure or truncation leaves the
        guard unresolved: callers see ``ExclusionUnavailable`` (its ``__cause__`` the
        failure, when the listing raised)."""
        self.require()
        if self._bound:
            return
        url = grammar.url("graph.users", {"$select": _DIRECTORY_SELECT, "$top": "999"})
        try:
            users, completeness = api.get_pages(url, max_pages=50, max_items=5000)
        except Exception as exc:  # noqa: BLE001 - any failure leaves the guard closed
            self._bind_failed = True
            # The cause (e.g. a refused token) stays on the chain so a caller can
            # report the upstream refusal; the listing URL names nobody.
            raise ExclusionUnavailable("excluded_people_unresolved") from exc
        if not completeness.get("complete"):
            self._bind_failed = True
            raise ExclusionUnavailable("excluded_people_unresolved")
        addresses, onedrive = set(), set()
        listing: list[tuple[str, str | None, list[str]]] = []
        for user in users:
            oid = user.get("id") if isinstance(user, Mapping) else None
            if not is_object_id(oid):
                continue
            upn, mail = user.get("userPrincipalName"), user.get("mail")
            held = [value.strip().lower() for value in (upn, mail)
                    if isinstance(value, str) and value.strip()]
            held += [proxy[5:].strip().lower() for proxy in user.get("proxyAddresses") or ()
                     if isinstance(proxy, str) and proxy.lower().startswith("smtp:")]
            listing.append((oid.lower(), upn.strip().lower() if isinstance(upn, str)
                            and upn.strip() else None, held))
            if oid.lower() not in self._ids:
                continue
            addresses.update(held)
            if isinstance(upn, str) and upn:
                onedrive.add(onedrive_path(upn))
        self._addresses = frozenset(addresses)
        self._onedrive = frozenset(onedrive)
        self._listing, self._listing_at = listing, datetime.now(timezone.utc)
        self._bound = True

    def attach_directory(self, users: Iterable[tuple[Any, Any, Iterable[Any]]]) -> None:
        """Decide addresses offline from a captured directory listing: ``users`` is (object
        id, that user's UPN, its other identities: SMTP addresses). An address or UPN the
        listing maps to exactly one object id is decided by that id (addresses are unique
        in the tenant, so one held by a user the list does not exclude is no excluded
        person's); a OneDrive path only by the UPN it is made from (as ``bind`` does: an
        alias whose path form matches another user's OneDrive decides nothing); anything
        the listing does not map, or maps to two ids, stays undecided (withheld). Replaces
        any directory attached before."""
        addresses: dict[str, str] = {}
        onedrive: dict[str, str] = {}
        clashes: set[str] = set()

        def held(table: dict[str, str], key: str, oid: str) -> None:
            if table.get(key, oid) != oid:
                clashes.add(key)
            table[key] = oid

        for oid, upn, identities in users:
            if not is_object_id(oid):
                continue
            oid = oid.lower()
            for value in (upn, *(identities or ())):
                if isinstance(value, str) and "@" in value:
                    held(addresses, value.strip().lower(), oid)
            if isinstance(upn, str) and "@" in upn:
                held(onedrive, onedrive_path(upn.strip().lower()), oid)
        for key in clashes:
            addresses.pop(key, None)
            onedrive.pop(key, None)
        self._directory, self._directory_onedrive = addresses, onedrive

    def list_groups(self, api: Any) -> None:
        """Read the directory's groups' addresses (``GET /groups``, mail and SMTP proxy
        addresses, every page) once bound, so a recorded listing can clear a group or
        distribution list. A failed or truncated listing records no group (their
        addresses then stay undecided offline); it never makes the guard unavailable."""
        self.require_bound()
        url = grammar.url("graph.groups", {"$select": "id,mail,proxyAddresses",
                                            "$top": "999"})
        try:
            groups, completeness = api.get_pages(url, max_pages=50, max_items=20000)
        except Exception:  # noqa: BLE001 - no groups: nothing is cleared by them
            self._groups = None
            return
        if not completeness.get("complete"):
            self._groups = None
            return
        found = set()
        for group in groups:
            if not isinstance(group, Mapping):
                continue
            mail = group.get("mail")
            if isinstance(mail, str) and "@" in mail:
                found.add(mail.strip().lower())
            found.update(proxy[5:].strip().lower() for proxy in group.get("proxyAddresses") or ()
                         if isinstance(proxy, str) and proxy.lower().startswith("smtp:"))
        self._groups = frozenset(found)

    def attach_listing(self, rows: Iterable[tuple[Any, Any, Any]]) -> None:
        """Decide addresses offline from a recorded tenant listing (``tenant_listing_rows``):
        ``rows`` is (address, object id, kind) with kind ``user`` (a cleared listed user's
        address and id) or ``group`` (a group's address, no id). An address is decided only
        by membership: a user's by its id, a group's as no person's. An address held twice
        or malformed decides nothing; any other address stays undecided. Replaces any
        listing attached before; known domains are unchanged (a listing names guests too)."""
        users: dict[str, str] = {}
        groups: set[str] = set()
        seen: set[str] = set()
        clashes: set[str] = set()
        for address, oid, kind in rows:
            if not isinstance(address, str) or "@" not in address:
                continue
            address = address.strip().lower()
            if address in seen:
                clashes.add(address)
            seen.add(address)
            if kind == "user" and is_object_id(oid):
                users[address] = oid.lower()
            elif kind == "group" and oid is None:
                groups.add(address)
            else:
                clashes.add(address)
        for address in clashes:
            users.pop(address, None)
            groups.discard(address)
        self._tenant, self._tenant_groups = users, frozenset(groups)

    def known_domains(self) -> frozenset[str]:
        """The mail domains the tenant's directory (attached or bound) holds addresses in:
        an address there is a tenant person's and must be decided, never assumed external."""
        held = set(self._directory or ()) | set(self._addresses)
        return frozenset(a.rsplit("@", 1)[1] for a in held if "@" in a)

    def object_id_of(self, address: Any) -> str | None:
        """The object id the attached directory maps ``address`` to (one user), else None."""
        if self._directory is None or not isinstance(address, str):
            return None
        return self._directory.get(address.strip().lower())

    @property
    def directory_loaded(self) -> bool:
        """A captured directory or recorded tenant listing is attached (it decides what
        it maps)."""
        return self._directory is not None or self._tenant is not None

    def _listing_decides(self, lowered: str) -> bool:
        return self._tenant is not None and (lowered in self._tenant
                                             or lowered in self._tenant_groups)

    def decides(self, value: Any) -> bool:
        """Whether ``is_excluded`` can decide ``value`` now (an object id; an address once
        bound, or one the attached directory maps)."""
        if self.unavailable or not isinstance(value, str) or not value.strip():
            return False
        lowered = value.strip().lower()
        return is_object_id(lowered) or self._bound or (
            self._directory is not None and lowered in self._directory) \
            or self._listing_decides(lowered)

    # ---------------------------------------------------------------- checks

    def is_excluded(self, value: Any) -> bool:
        """Whether one object ID or address is an excluded person's. An address is
        decided once bound, or by the attached directory's object id for it; otherwise
        the guard refuses to decide one."""
        self.require()
        if not isinstance(value, str) or not value:
            return False
        lowered = value.strip().lower()
        if is_object_id(lowered):
            return lowered in self._ids
        if self._bound:
            return lowered in self._addresses
        oid = None if self._directory is None else self._directory.get(lowered)
        if oid is not None and oid in self._ids:
            return True
        if self._listing_decides(lowered):
            # A cleared user's address: excluded when the list names that user now; a
            # group's address is no person's.
            held = self._tenant.get(lowered)
            return held is not None and held in self._ids
        if oid is None:
            raise ExclusionUnavailable("excluded_people_unresolved")
        return False

    def excluded(self, identities: Iterable[Any]) -> bool:
        return any(self.is_excluded(value) for value in identities)

    def refuse(self, identities: Iterable[Any], kind: str) -> None:
        """Raise ``PeopleExcluded(kind)`` when any identity is an excluded person's."""
        if self.excluded(identities):
            raise PeopleExcluded(kind)

    def refuse_onedrive_path(self, path: str | None) -> None:
        """Refuse a personal site whose ``/personal/<segment>`` is an excluded UPN's."""
        if not self._bound and self._directory is not None:
            # The attached directory: the path must be a listed user's, not an excluded one.
            self.require()
            if not isinstance(path, str):
                return
            oid = self._directory_onedrive.get(path.lower())
            if oid is None:
                raise ExclusionUnavailable("excluded_people_unresolved")
            if oid in self._ids:
                raise PeopleExcluded("onedrive_owner")
            return
        self.require_bound()
        if isinstance(path, str) and path.lower() in self._onedrive:
            raise PeopleExcluded("onedrive_owner")

    def drop(self) -> None:
        """Count one message dropped for its sender (the count is the only trace)."""
        self.dropped += 1

    def block_part(self, part: str) -> dict[str, Any]:
        """The ``blocked_upstream`` part of a run the guard refused (codes, no IDs)."""
        code = self.unavailable or "excluded_people_unresolved"
        return {"part": part, "source": "people-exclusion", "http_status": None,
                "error_code": code,
                "cause": ("upstream_outage" if code == "excluded_people_unresolved"
                          else "later_ask"), "later_ask": LATER_ASK}


class ReloadingExclusion:
    """A ``PeopleExclusion`` re-read from the environment's sources when the list file
    changes (inode, size, mtime; checked on every ``current()``) or ``ttl`` seconds pass,
    so an added person applies without a restart. A failed re-read is the unavailable
    guard (fail closed). Attribute reads go to the current guard."""

    def __init__(self, environ: Mapping[str, str], *, ttl: float = 300.0,
                 config: Path = CONFIG, clock: Any = None, state_dir: Any = None) -> None:
        import time
        self._state_dir = state_dir   # the mail capture's directory is attached from it
        self._environ = dict(environ)
        self._ttl, self._config = ttl, config
        self._clock = clock or time.monotonic
        self._stamp: Any = None
        self._at = 0.0
        self._guard: PeopleExclusion | None = None
        self.current()

    @classmethod
    def from_env(cls, environ: Mapping[str, str], **options: Any) -> "ReloadingExclusion":
        return cls(environ, **options)

    def _file_stamp(self) -> Any:
        stamps = []
        for path in (self._environ.get(ENV_FILE), str(self._config)):
            try:
                info = Path(path).expanduser().stat() if path else None
            except OSError:
                info = None
            stamps.append(None if info is None else
                          (info.st_ino, info.st_size, info.st_mtime_ns))
        return tuple(stamps)

    def current(self) -> PeopleExclusion:
        now, stamp = self._clock(), self._file_stamp()
        if self._guard is None or stamp != self._stamp or now - self._at >= self._ttl:
            try:
                self._guard = with_directory(
                    PeopleExclusion.from_env(self._environ, config=self._config),
                    self._state_dir)
            except Exception:  # noqa: BLE001 - an unreadable list refuses everything
                self._guard = PeopleExclusion(unavailable="excluded_people_file_unreadable")
            self._stamp, self._at = stamp, now
        return self._guard

    def __getattr__(self, name: str) -> Any:
        return getattr(self.current(), name)

    def __repr__(self) -> str:
        return f"ReloadingExclusion({self.current()!r})"


# A captured mailbox identity checked longer ago than this decides nothing (an address
# may have been given to someone else since): ``mail_capture_directory``.
DIRECTORY_MAX_AGE = timedelta(days=7)


def mail_capture_directory(state_dir: Any, *, now: datetime | None = None
                           ) -> list[tuple[str, str, list[str]]] | None:
    """The directory the mail capture recorded under ``state_dir`` (``mail-capture``:
    each captured mailbox's object id, UPN and SMTP addresses), read-only: only the
    mailboxes whose identity was checked within ``DIRECTORY_MAX_AGE`` of ``now``. None
    when there is no capture, it cannot be read, or none of it is that recent (not loaded:
    nothing is then decided by it, so what only it could decide stays withheld)."""
    import sqlite3
    if state_dir is None:
        return None
    path = Path(state_dir) / "mail-capture" / "capture.db"
    if not path.is_file():
        return None
    now = now or datetime.now(timezone.utc)
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
        try:
            rows = db.execute("SELECT mailbox_id, upn, identities, identity_checked_at "
                              "FROM mailboxes").fetchall()
        finally:
            db.close()
        users = []
        for oid, upn, identities, checked in rows:
            try:
                at = datetime.fromisoformat(str(checked).replace("Z", "+00:00"))
            except ValueError:
                continue   # undated: decides nothing
            if at.tzinfo is None or not now - DIRECTORY_MAX_AGE <= at <= now:
                continue   # past the bound, or in the future
            users.append((oid, upn, list(json.loads(identities))))
        return users or None
    except (sqlite3.Error, ValueError, TypeError, OSError):
        return None


def tenant_listing_rows(guard: PeopleExclusion
                        ) -> tuple[str, list[tuple[str, str | None, str]]] | None:
    """(read at, rows) of the complete listing ``guard`` was bound with, to record offline
    (``attach_listing``): (address, object id, ``user``) for each address of a listed user
    the exclusion does not name, and (address, None, ``group``) for each address of the
    groups listing (``list_groups``, when read). An address an excluded user holds, or that
    two entries hold, is left out (undecided offline): nothing about an excluded person is
    recorded. None when the guard is not bound or unavailable."""
    if not isinstance(guard, PeopleExclusion) or guard.unavailable or not guard.bound \
            or guard._listing is None or guard._listing_at is None:
        return None
    entries: list[tuple[str, str | None, str]] = []
    withheld: set[str] = set(guard._addresses)
    for oid, _upn, held in guard._listing:
        if oid in guard._ids:
            withheld.update(held)
            continue
        entries.extend((address, oid, "user") for address in set(held))
    entries.extend((address, None, "group") for address in guard._groups or ())
    count: dict[str, int] = {}
    for address, _oid, _kind in entries:
        count[address] = count.get(address, 0) + 1
    rows = sorted(e for e in entries if count[e[0]] == 1 and e[0] not in withheld)
    at = guard._listing_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return at, rows


def mail_capture_listing(state_dir: Any, *, now: datetime | None = None
                         ) -> list[tuple[str, str | None, str]] | None:
    """The rows of the tenant listing the mail capture under ``state_dir`` recorded
    (``tenant_listing_rows``), read-only, when it was read within ``DIRECTORY_MAX_AGE`` of
    ``now``; None when there is none, it cannot be read, or it is older (decides nothing)."""
    import sqlite3
    if state_dir is None:
        return None
    path = Path(state_dir) / "mail-capture" / "capture.db"
    if not path.is_file():
        return None
    now = now or datetime.now(timezone.utc)
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
        try:
            db.execute("BEGIN")   # one snapshot: the rows and their read time
            found = db.execute("SELECT value FROM meta WHERE key='tenant_listing_at'"
                               ).fetchone()
            if found is None:
                return None
            at = datetime.fromisoformat(str(found[0]).replace("Z", "+00:00"))
            if at.tzinfo is None or not now - DIRECTORY_MAX_AGE <= at <= now:
                return None
            rows = db.execute("SELECT address, object_id, kind FROM tenant_listing").fetchall()
        finally:
            db.close()
    except (sqlite3.Error, ValueError, TypeError, OSError):
        return None
    return rows or None


def with_directory(guard: Any, state_dir: Any) -> Any:
    """A copy of ``guard`` (a ``PeopleExclusion``) with what the mail capture under
    ``state_dir`` recorded attached (its mailboxes' directory; its last tenant listing),
    when it is unbound and has none; else ``guard`` itself."""
    import copy
    if not isinstance(guard, PeopleExclusion) or guard.bound or guard.directory_loaded:
        return guard
    users = mail_capture_directory(state_dir)
    listing = mail_capture_listing(state_dir)
    if users is None and listing is None:
        return guard
    held = copy.copy(guard)
    if users is not None:
        held.attach_directory(users)
    if listing is not None:
        held.attach_listing(listing)
    return held


def from_api(api: Any) -> PeopleExclusion:
    """The guard ``build_graph`` attached to ``api``; a missing one is unavailable."""
    found = getattr(api, "people", None)
    if isinstance(found, PeopleExclusion):
        return found
    return PeopleExclusion(unavailable="excluded_people_oids_absent")
