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
address-named read fails closed too. Nothing here prints, logs or returns an excluded
object ID or address: errors, reprs and reports carry codes and counts only.
"""

from __future__ import annotations

import json
import re
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
            values.extend(v for v in re.split(r"[\s,;]+", raw) if v)
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
        for user in users:
            oid = user.get("id")
            if not is_object_id(oid) or oid.lower() not in self._ids:
                continue
            upn, mail = user.get("userPrincipalName"), user.get("mail")
            for value in (upn, mail):
                if isinstance(value, str) and value:
                    addresses.add(value.lower())
            for proxy in user.get("proxyAddresses") or ():
                if isinstance(proxy, str) and proxy.lower().startswith("smtp:"):
                    addresses.add(proxy[5:].lower())
            if isinstance(upn, str) and upn:
                onedrive.add(onedrive_path(upn))
        self._addresses = frozenset(addresses)
        self._onedrive = frozenset(onedrive)
        self._bound = True

    # ---------------------------------------------------------------- checks

    def is_excluded(self, value: Any) -> bool:
        """Whether one object ID or address is an excluded person's. An address is
        decided only once bound; an unbound guard refuses to decide one."""
        self.require()
        if not isinstance(value, str) or not value:
            return False
        lowered = value.strip().lower()
        if is_object_id(lowered):
            return lowered in self._ids
        if not self._bound:
            raise ExclusionUnavailable("excluded_people_unresolved")
        return lowered in self._addresses

    def excluded(self, identities: Iterable[Any]) -> bool:
        return any(self.is_excluded(value) for value in identities)

    def refuse(self, identities: Iterable[Any], kind: str) -> None:
        """Raise ``PeopleExcluded(kind)`` when any identity is an excluded person's."""
        if self.excluded(identities):
            raise PeopleExcluded(kind)

    def refuse_onedrive_path(self, path: str | None) -> None:
        """Refuse a personal site whose ``/personal/<segment>`` is an excluded UPN's."""
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


def from_api(api: Any) -> PeopleExclusion:
    """The guard ``build_graph`` attached to ``api``; a missing one is unavailable."""
    found = getattr(api, "people", None)
    if isinstance(found, PeopleExclusion):
        return found
    return PeopleExclusion(unavailable="excluded_people_oids_absent")
