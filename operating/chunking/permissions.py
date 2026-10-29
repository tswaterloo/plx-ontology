"""Permission keys of a chunk and the people exclusion, decided before any text is read.

Keys (``key_of``): a chunk carries its source item's audience unit exactly as the
projection keys the object (``objects.audience_key``; ``ReadPolicy`` grants these units
and ``plx.find`` filters on them in SQL) and the union of the markings ``chunk_matches``
requires (text field, current version, latest version). A case original carries its
source unit's key (``captured_originals.source_unit``: a SharePoint item's drive, a Teams
chat, a repository) and its ``original://`` citation, which the original's own gate
re-checks per reader. A unit outside the known families, a wildcard, an empty unit or a
marking the store does not know is no key: the chunk is not written (counted).

People exclusion (``Exclusion``; ``integrations.people_exclusion``): IDs come from
``PLX_EXCLUDED_PEOPLE_OIDS`` / ``_FILE`` and the committed config. An unavailable guard
refuses every person-sourced family (mail, Teams, SharePoint incl. lists, originals):
nothing of them is chunked. Offline (no Graph call here) the guard decides an object ID
directly; an address or a OneDrive owner only through the captured directory (the mail
capture's mailbox rows: object ID, UPN, every SMTP address). An address in a tenant domain
the directory does not hold, a OneDrive whose owner it does not hold, and a Teams chat
whose members the Teams capture did not record are undecidable and withheld (counted),
never chunked.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import unquote, urlsplit

from integrations.people_exclusion import PeopleExclusion, is_object_id, onedrive_path
from ontology.store_ddl import MARKINGS

# Audience families a ReadPolicy may grant (CONTRACT: api:/bc:/mail:/sharepoint:/teams:/
# mc:/original:/repo:; plus the projection's sharepoint-list:, workbench:, brain:).
FAMILIES = frozenset({"api", "bc", "mail", "sharepoint", "sharepoint-list", "teams", "mc",
                      "original", "repo", "workbench", "brain"})
# Families whose items are a person's mail, chats or files: the exclusion gates them.
PERSON_FAMILIES = frozenset({"mail", "teams", "sharepoint", "sharepoint-list", "original",
                             "repo"})


def family(key: str) -> str:
    return key.split(":", 1)[0] if isinstance(key, str) else ""


def valid_key(key: Any) -> bool:
    """An exact audience unit of a known family (no wildcard, no empty unit)."""
    if not isinstance(key, str) or ":" not in key or "*" in key or key != key.strip():
        return False
    head, unit = key.split(":", 1)
    return head in FAMILIES and bool(unit.strip())


def parse_markings(*texts: Any) -> tuple[str, ...] | None:
    """The union of JSON marking lists; None when one is unreadable or names a marking
    the store does not know (fail closed)."""
    out: set[str] = set()
    for text in texts:
        if text is None:
            continue
        try:
            values = json.loads(text) if isinstance(text, str) else list(text)
        except (TypeError, ValueError):
            return None
        if not isinstance(values, list) or any(v not in MARKINGS for v in values):
            return None
        out.update(values)
    return tuple(sorted(out))


class Exclusion:
    """The people exclusion as the chunker applies it offline (module docstring)."""

    def __init__(self, guard: PeopleExclusion, *, directory: Mapping[str, str] | None = None,
                 internal_domains: Iterable[str] = (),
                 drive_owners: Mapping[str, str | None] | None = None,
                 chat_members: Mapping[str, tuple[str, ...]] | None = None,
                 drive_sites: Mapping[str, str] | None = None) -> None:
        self.guard = guard
        self.directory = {k.lower(): v.lower() for k, v in (directory or {}).items()}
        self.segments = {onedrive_path(addr): oid for addr, oid in self.directory.items()
                         if "@" in addr}
        self.internal = frozenset(d.lower() for d in internal_domains)
        # drive id -> its OneDrive owner segment ("" for a site library); absent: unknown.
        self.drive_owners = dict(drive_owners or {})
        self.chat_members = dict(chat_members or {})
        self.drive_sites = dict(drive_sites or {})   # drive id -> its site's name

    @property
    def unavailable(self) -> str | None:
        return self.guard.unavailable

    def _oid_clear(self, oid: str) -> bool:
        try:
            return not self.guard.is_excluded(oid)
        except Exception:  # noqa: BLE001 - an undecidable guard withholds
            return False

    def identity_clear(self, value: Any) -> bool:
        """An object ID, or an address decided through the captured directory: True only
        when decided and not excluded. An external-domain address is no person of ours."""
        if not isinstance(value, str) or not value.strip():
            return True
        lowered = value.strip().lower()
        if is_object_id(lowered):
            return self._oid_clear(lowered)
        if "@" not in lowered:
            return False
        if lowered in self.directory:
            return self._oid_clear(self.directory[lowered])
        domain = lowered.rsplit("@", 1)[1]
        return bool(domain) and not any(domain == d or domain.endswith("." + d)
                                        for d in self.internal)

    def onedrive_clear(self, segment: str | None) -> bool:
        if not segment:
            return True
        oid = self.segments.get(segment.lower())
        return oid is not None and self._oid_clear(oid)

    def unit_clear(self, audience_key: str) -> bool:
        """May anything of this audience unit be chunked at all?"""
        head = family(audience_key)
        if head not in PERSON_FAMILIES:
            return True
        if self.unavailable:
            return False
        unit = audience_key.split(":", 1)[1]
        if head == "mail":
            return self._oid_clear(unit) if is_object_id(unit) else False
        if head == "sharepoint":
            if unit not in self.drive_owners:
                return False
            return self.onedrive_clear(self.drive_owners[unit])
        if head == "sharepoint-list":
            return "-my.sharepoint.com" not in unit.lower()
        if head == "teams":
            if unit.startswith("chat:"):
                members = self.chat_members.get(unit[len("chat:"):])
                return members is not None and all(self.identity_clear(m) for m in members)
            return unit.startswith("channel:")
        return True


def unit_row(key: str, exclusion: Exclusion) -> dict[str, Any]:
    """The ``search.audience_units`` row of one unit.

    ``shared`` only for Portal/Sage relation families (``api:``), BC companies (``bc:``),
    SharePoint sites, libraries and lists, Teams channels and MC scopes, with a human label;
    everything else ``personal`` with no label. ``people``: the Entra object ids the unit
    belongs to or whose membership defines it (a mailbox's owner, a OneDrive's owner, a
    chat's members), never shown; the search layer drops a unit naming an excluded person.
    """
    head = family(key)
    unit = key.split(":", 1)[1] if ":" in key else ""

    def personal(people: Iterable[str] = ()) -> dict[str, Any]:
        return {"audience_key": key, "sharing": "personal", "label": None,
                "people": sorted({p.lower() for p in people if is_object_id(p)})}

    def shared(label: str) -> dict[str, Any]:
        return {"audience_key": key, "sharing": "shared", "label": label, "people": []}

    if head == "mail":
        return personal([unit])
    if head == "teams" and unit.startswith("chat:"):
        members = exclusion.chat_members.get(unit[len("chat:"):]) or ()
        return personal([m for m in members if isinstance(m, str) and is_object_id(m)]
                        + [exclusion.directory[m.lower()] for m in members
                           if isinstance(m, str) and m.lower() in exclusion.directory])
    if head == "teams" and unit.startswith("channel:"):
        return shared("Teams channel")
    if head == "sharepoint":
        segment = exclusion.drive_owners.get(unit)
        if segment == "":
            return shared(exclusion.drive_sites.get(unit) or "SharePoint site")
        owner = exclusion.segments.get(segment.lower()) if segment else None
        return personal([owner] if owner else [])
    if head == "sharepoint-list":
        return personal() if "-my.sharepoint.com" in unit.lower() else shared("SharePoint list")
    if head == "api":
        source, _, relation = unit.partition(".")
        system = {"public": "Customer Portal", "sage": "Sage"}.get(source, source or "Portal")
        name = relation.removeprefix("app_").replace("_", " ").strip()
        return shared(f"{system}: {name}" if name else system)
    if head == "bc":
        entity = unit.split("/", 1)[1] if "/" in unit else ""
        return shared("Business Central" + (f": {entity}" if entity else ""))
    if head == "mc":
        scope = unit.split("/")
        return shared("Mission Control" + (f": {scope[-1]}" if len(scope) > 1 else ""))
    return personal()


def onedrive_segment(url: Any) -> str | None:
    """``/personal/<segment>/`` of a OneDrive URL (lower-cased); "" for any other site;
    None when the URL is unreadable."""
    if not isinstance(url, str) or "://" not in url:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    path = unquote(parts.path)
    if (parts.hostname or "").endswith("-my.sharepoint.com"):
        if not path.lower().startswith("/personal/"):
            return None
        segment = path.split("/")[2] if len(path.split("/")) > 2 else ""
        return segment.lower() or None
    return ""


def _ro(path: Path) -> sqlite3.Connection | None:
    if not path.is_file():
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def load_exclusion(state_dir: str | Path, environ: Mapping[str, str] | None = None
                   ) -> Exclusion:
    """The guard from the environment plus what the captures recorded offline: the mail
    capture's directory (mailbox ids and addresses), the SharePoint capture's drive owners
    and the Teams capture's chat members."""
    environ = os.environ if environ is None else environ
    guard = PeopleExclusion.from_env(environ)
    state = Path(state_dir)
    directory: dict[str, str] = {}
    domains: set[str] = set()
    mail = _ro(state / "mail-capture" / "capture.db")
    if mail is not None:
        with mail:
            for row in mail.execute("SELECT mailbox_id, upn, identities FROM mailboxes"):
                addresses = [row["upn"]]
                try:
                    addresses += [a for a in json.loads(row["identities"]) if isinstance(a, str)]
                except ValueError:
                    pass
                for address in addresses:
                    if isinstance(address, str) and "@" in address:
                        address = address.split(":", 1)[-1] if address.lower().startswith(
                            "smtp:") else address
                        directory[address.lower()] = row["mailbox_id"]
                        domains.add(address.rsplit("@", 1)[1].lower())
        mail.close()
    owners: dict[str, str | None] = {}
    sites: dict[str, str] = {}
    sharepoint = _ro(state / "sharepoint-capture" / "capture.db")
    if sharepoint is not None:
        with sharepoint:
            for row in sharepoint.execute(
                    "SELECT drive_id, json_extract(record, '$.original_url') AS url FROM items "
                    "GROUP BY drive_id"):
                segment = onedrive_segment(row["url"])
                if segment is not None:
                    owners[row["drive_id"]] = segment
                if segment == "":
                    parts = unquote(urlsplit(row["url"]).path).split("/")
                    if len(parts) > 2 and parts[1].lower() in ("sites", "teams") and parts[2]:
                        sites[row["drive_id"]] = parts[2]
        sharepoint.close()
    members: dict[str, tuple[str, ...]] = {}
    teams = _ro(state / "teams-capture" / "capture.db")
    if teams is not None:
        with teams:
            for row in teams.execute("SELECT unit, acl FROM unit_acl WHERE kind = 'teams_chat'"):
                try:
                    acl = json.loads(row["acl"])
                except ValueError:
                    continue
                unit = row["unit"]
                chat = unit[len("teams:chat:"):] if unit.startswith("teams:chat:") else None
                if chat is None or not isinstance(acl, Mapping):
                    continue
                ids = list((acl.get("history_start") or {}).keys()) + [
                    i for i in acl.get("identities") or () if isinstance(i, str)]
                members[chat] = tuple(ids)
        teams.close()
    return Exclusion(guard, directory=directory, internal_domains=domains,
                     drive_owners=owners, chat_members=members, drive_sites=sites)


__all__ = ["FAMILIES", "PERSON_FAMILIES", "Exclusion", "family", "load_exclusion",
           "onedrive_segment", "parse_markings", "unit_row", "valid_key"]
