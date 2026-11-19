"""The staff read scope.

Every employee who is not a full reader ("staff") reads the company's business records
whole and their own personal sources:

* business families (``api:``, ``bc:``, ``mc:``, ``sharepoint-list:``) by their wildcard;
* ``sharepoint:`` units shared by nature (``search.audience_units.sharing = 'shared'``) and
  the reader's own OneDrive (a personal unit whose ``people`` is the reader);
* ``teams:`` public channels (a shared ``teams:channel:`` unit) and the chats and channels
  whose ``people`` hold the reader;
* ``mail:`` the reader's own mailbox and every shared mailbox (``shared_mailboxes``: the
  kind the mail capture's directory listing records) not listed as restricted
  (``RESTRICTED_SHARED_ENV``); never another person's personal mailbox;
* ``org:chart`` (``ORG_CHART_KEY``): the org chart, with a usable people exclusion;
* ``meetings:`` the reader's own calendar and transcripts, and every Planner plan (plans
  are business records: a plan's unit names no members, so its people decide nothing but
  the exclusion).

The people exclusion decides on top: any unit whose ``people`` names an excluded person,
and a mailbox or calendar of one, is denied by an exact entry. When the exclusion is not
usable only the business families are granted, and a business unit that names any person
is denied (fail closed).

The decision is made once per session from precomputed rows (``load_units``: one read of
``search.audience_units`` per search write generation), with no live audience call and no
per-unit check. A key with no row is decided by its family's rule alone, which grants a
personal family nothing. Stdlib only.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import re
import sqlite3
import threading
from typing import Any, Callable, Iterable, Mapping

# The families every employee reads whole.
BUSINESS_FAMILIES = ("api", "bc", "mc", "sharepoint-list")
# The org chart (``biz.staff``: name, title, department, office, manager, reports), read by
# every staff reader (Staff read all business records). No store
# object carries it: the business view's rows do, and ``ontology_tools_policy.reader_grants``
# offers it to every reader's policy beside the store's keys. Granted only with a usable
# people exclusion; a guest or unresolved reader's policy grants it nothing.
ORG_CHART_KEY = "org:chart"
# Shared mailboxes that stay with the readers who hold them today (HR, payroll,
# finance-only boxes): a comma-separated list of mailbox object ids or addresses. A
# listed mailbox is never opened by the staff rule (only a full-read grant reads it).
RESTRICTED_SHARED_ENV = "PLX_RESTRICTED_SHARED_MAILBOXES"
# Mailboxes a directory listing once recorded as a person's that may read as shared again
# (a comma-separated list of mailbox object ids, nothing else): without an entry, a mailbox
# once personal (a departed employee's whose name an admin cleared) never opens to staff.
SHARED_ALLOW_ENV = "PLX_SHARED_MAILBOX_ALLOW"
# Sensitive SharePoint sites (payroll, executive finance, tax, personal) that keep their
# per-file permission check under the staff scope: a comma-separated list of site names (the
# ``/sites/<name>/`` segment of a file's URL, matched case-insensitively), drive ids
# (``b!...``) or folders (``<site name>/<folder>[/<folder>...]``: that folder run anywhere in
# the site's paths). Unset or naming no valid entry: EVERY site keeps its per-file check (the
# staff file rule never fires). On any site, a file under a pay, HR or confidential path
# (``sensitive_path``) or with permissions narrower than its library's and site's inherited
# ones (``inherits_site``) keeps its per-file check too. The deployed list: deploy/systemd/host/plx-api.service.d/
# staff-scope.conf.
RESTRICTED_SITES_ENV = "PLX_RESTRICTED_SITES"
_SITE_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_DRIVE_ID = re.compile(r"^b![a-z0-9_-]{8,}$")
_GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_ADDRESS = re.compile(r"^[^@\s,]+@[^@\s,]+\.[^@\s,]+$")


@dataclass(frozen=True)
class Unit:
    """One ``search.audience_units`` row: its sharing and the people it names."""
    sharing: str
    people: frozenset[str] = frozenset()


def restricted_shared(value: Any = None) -> frozenset[str]:
    """``RESTRICTED_SHARED_ENV``'s entries (lower-cased object ids and addresses); an entry
    that is neither is ignored. Read from the environment when ``value`` is None."""
    text = os.environ.get(RESTRICTED_SHARED_ENV, "") if value is None else value
    out = set()
    for entry in str(text or "").split(","):
        entry = entry.strip().lower()
        if _GUID.match(entry) or _ADDRESS.match(entry):
            out.add(entry)
    return frozenset(out)


def shared_allowed(value: Any = None) -> frozenset[str]:
    """``SHARED_ALLOW_ENV``'s entries: lower-cased mailbox object ids (anything else is
    ignored). Read from the environment when ``value`` is None."""
    text = os.environ.get(SHARED_ALLOW_ENV, "") if value is None else value
    return frozenset(e.strip().lower() for e in str(text or "").split(",")
                     if _GUID.match(e.strip().lower()))


def shared_mailboxes(conn: Any, restricted: Iterable[str] | None = None,
                     allowed: Iterable[str] | None = None) -> frozenset[str]:
    """The mailbox object ids of a mail capture (``conn``: its SQLite connection) that every
    employee reads: a captured mailbox whose ``mailbox_purpose`` row is exactly ``shared``
    (``integrations.capture_mail.mailbox_kind``, recorded by the directory listing) and
    that no listing ever recorded as a person's (``personal_at``) unless its id is allowed
    (``allowed``, else ``SHARED_ALLOW_ENV``), minus every restricted one (``restricted``,
    else ``RESTRICTED_SHARED_ENV``: matched on the id, the UPN or any recorded address). A
    mailbox with no such row, or a capture that records none or cannot be read, names none
    (fail closed: unknown is personal)."""
    import json
    held = restricted_shared() if restricted is None else frozenset(
        str(v).strip().lower() for v in restricted)
    allow = shared_allowed() if allowed is None else frozenset(
        str(v).strip().lower() for v in allowed)
    try:
        rows = conn.execute(
            "SELECT m.mailbox_id, m.upn, m.identities, p.personal_at FROM mailboxes m "
            "JOIN mailbox_purpose p ON p.mailbox_id = lower(m.mailbox_id) "
            "WHERE p.purpose = 'shared'").fetchall()
    except (sqlite3.Error, AttributeError):
        return frozenset()
    out = set()
    for mailbox_id, upn, identities, personal_at in rows:
        if not isinstance(mailbox_id, str) or not _GUID.match(mailbox_id.strip().lower()):
            continue
        if personal_at is not None and mailbox_id.strip().lower() not in allow:
            continue   # once a person's: never shared without an explicit allow entry
        names = {mailbox_id.strip().lower(), str(upn or "").strip().lower()}
        try:
            listed = json.loads(identities or "[]")
            names |= {str(i).strip().lower() for i in listed if isinstance(i, str)}
        except (TypeError, ValueError):
            continue   # an unreadable address list cannot be told from a restricted box
        if not names & held:
            out.add(mailbox_id.strip().lower())
    return frozenset(out)


def restricted_sites(value: Any = None) -> frozenset[str] | None:
    """``RESTRICTED_SITES_ENV``'s entries (lower-cased site names and drive ids); an entry
    that is neither is ignored. None when the setting is unset or names no valid entry:
    then every site is restricted (fail closed). Read from the environment when ``value``
    is None."""
    text = os.environ.get(RESTRICTED_SITES_ENV, "") if value is None else value
    out = set()
    for entry in str(text or "").split(","):
        entry = entry.strip().lower()
        if _SITE_NAME.match(entry) or _DRIVE_ID.match(entry):
            out.add(entry)
    return frozenset(out) or None


def site_of(record: Any) -> tuple[str, str] | None:
    """(site name, drive id), lower-cased, of a SharePoint drive-item record: the name is
    the ``/sites/<name>/`` or ``/teams/<name>/`` segment of its ``original_url``. None when
    either cannot be read (a OneDrive's ``/personal/`` URL included)."""
    from urllib.parse import unquote, urlsplit
    if not isinstance(record, Mapping):
        return None
    url, drive = record.get("original_url"), record.get("drive_id")
    if not isinstance(url, str) or not isinstance(drive, str) or not drive.strip():
        return None
    try:
        parts = unquote(urlsplit(url.strip()).path).split("/")
    except ValueError:
        return None
    if len(parts) < 3 or parts[1].lower() not in ("sites", "teams") or not parts[2].strip():
        return None
    return parts[2].strip().lower(), drive.strip().lower()


def restricted_folders(value: Any = None) -> frozenset[tuple[str, tuple[str, ...]]]:
    """``RESTRICTED_SITES_ENV``'s folder entries: ``<site name>/<folder>[/<folder>...]``
    (lower-cased, matched case-insensitively), as (site, folder segments). A file of that
    site under such a folder run, anywhere in its path, keeps its per-file check. Read from
    the environment when ``value`` is None."""
    text = os.environ.get(RESTRICTED_SITES_ENV, "") if value is None else value
    out = set()
    for entry in str(text or "").split(","):
        site, _, rest = entry.strip().lower().partition("/")
        folders = tuple(s.strip() for s in rest.split("/") if s.strip())
        if _SITE_NAME.match(site.strip()) and folders:
            out.add((site.strip(), folders))
    return frozenset(out)


# Path words that keep a file's per-file permission check on any site (a tripwire under
# the site list, decision 2026-11-17 review): pay, HR and confidentiality folders and
# files. The Operations site's "_Restricted Payroll" folder carries the site's own
# permissions (nothing narrower), so only its name tells it apart.
_SENSITIVE_PATH = re.compile(
    r"payroll|pay[ ._-]?stubs?|pay[ ._-]?statements?|pay[ ._-]?cheques?|paychecks?"
    r"|salar(?:y|ies)|compensation|confidential|restricted|personnel|human[ ._-]?resources"
    r"|disciplin|employment|direct[ ._-]?deposit|void[ ._-]?cheque|r[eé]sum[eé]|appraisal"
    r"|performance[ ._-]?reviews?|(?:termination|offer)[ ._-]?letters?|medical|garnish"
    r"|sick[ ._-]?(?:leave|notes?)|(?<![a-z])(?:hr|wages?|t4s?|private)(?![a-z])", re.I)


def _path_segments(record: Mapping[str, Any]) -> list[str] | None:
    """A drive item's path below its site (library, folders, name; unquoted,
    lower-cased) from its ``original_url``, with its drive-relative ``path`` appended
    when recorded; None when the URL cannot be read."""
    from urllib.parse import unquote, urlsplit
    url = record.get("original_url")
    if not isinstance(url, str):
        return None
    try:
        parts = unquote(urlsplit(url.strip()).path).split("/")
    except ValueError:
        return None
    segments = [p.strip().lower() for p in parts[3:] if p.strip()]
    path = record.get("path")
    if isinstance(path, str):
        segments += [p.strip().lower() for p in unquote(path).split("/") if p.strip()]
    return segments


def sensitive_path(record: Any) -> bool:
    """Whether a SharePoint file's URL or path names a pay, HR or confidential folder or
    file (``_SENSITIVE_PATH``); an unreadable URL counts as sensitive (fail closed)."""
    if not isinstance(record, Mapping):
        return True
    segments = _path_segments(record)
    return segments is None or any(_SENSITIVE_PATH.search(s) for s in segments)


def site_restricted(record: Any, value: Any = None) -> bool:
    """Whether a SharePoint file keeps its per-file permission check under the staff
    scope: its site or drive is listed (``value``, the setting's text, else
    ``RESTRICTED_SITES_ENV``), a listed ``<site>/<folder>`` entry holds it, its path
    names a sensitive folder or file (``sensitive_path``), the list is not configured, or
    the file's site cannot be read (fail closed)."""
    held = restricted_sites(value)
    site = site_of(record)
    if held is None or site is None or set(site) & held or sensitive_path(record):
        return True
    segments = _path_segments(record) or []
    for name, folders in restricted_folders(value):
        if name == site[0] and any(tuple(segments[i:i + len(folders)]) == folders
                                   for i in range(len(segments))):
            return True
    return False


def principals(acl: Any) -> frozenset[str] | None:
    """The principals a complete item permission list names (lower-cased; sharing-link
    scopes aside, which only add readers), or None when the list is not complete
    (``status`` other than ``read``) or not readable."""
    if not isinstance(acl, Mapping) or acl.get("status") != "read":
        return None
    identities = acl.get("identities")
    if not isinstance(identities, (list, tuple)) \
            or not all(isinstance(i, str) for i in identities):
        return None
    return frozenset(i.strip().lower() for i in identities
                     if i.strip() and not i.strip().lower().startswith("link-scope:"))


# A library's (and its site's) inherited permissions are the principal set most of its
# current files carry; it is taken as the baseline only when at least this share of the
# files carry it or more (else the drive has no clear inherited set: every file keeps its
# per-file check).
BASELINE_MIN_SHARE = 0.5


def _mode(counts: Mapping[frozenset[str], int]) -> frozenset[str] | None:
    """The principal set most files carry (ties: the larger set, then the first in sort
    order), when non-empty and carried, or exceeded, by ``BASELINE_MIN_SHARE`` of them."""
    total = sum(counts.values())
    if not total:
        return None
    mode = max(counts, key=lambda s: (counts[s], len(s), sorted(s)))
    covered = sum(n for s, n in counts.items() if s >= mode)
    return mode if mode and covered >= BASELINE_MIN_SHARE * total else None


def site_baselines(rows: Iterable[tuple[Any, Any, Any, Any]]) -> dict[str, frozenset[str]]:
    """Per drive id (lower-cased), the inherited permissions a file must carry, or more,
    to open to staff: the union of its library's and its site's baseline principal sets
    (``_mode``). ``rows``: (drive id, an ``original_url`` of the drive, item permission
    list JSON, number of current files carrying it), as ``read_site_baselines`` reads them.
    A drive of no readable site, or whose library or site has no clear baseline, is
    absent: its files keep their per-file check."""
    import json
    drives: dict[str, dict[frozenset[str], int]] = {}
    site_of_drive: dict[str, str] = {}
    for drive_id, url, acl_text, count in rows:
        if not isinstance(drive_id, str) or not drive_id.strip():
            continue
        drive = drive_id.strip().lower()
        site = site_of({"original_url": url, "drive_id": drive})
        if site is None:
            continue
        site_of_drive[drive] = site[0]
        try:
            held = principals(json.loads(acl_text)) if isinstance(acl_text, str) else None
        except ValueError:
            held = None
        # an unreadable or partial list carries nothing (it counts against the baseline)
        key = frozenset() if held is None else held
        counts = drives.setdefault(drive, {})
        counts[key] = counts.get(key, 0) + max(0, int(count or 0))
    sites: dict[str, dict[frozenset[str], int]] = {}
    for drive, counts in drives.items():
        merged = sites.setdefault(site_of_drive[drive], {})
        for key, n in counts.items():
            merged[key] = merged.get(key, 0) + n
    site_modes = {site: _mode(counts) for site, counts in sites.items()}
    out = {}
    for drive, counts in drives.items():
        library, site = _mode(counts), site_modes.get(site_of_drive[drive])
        if library is not None and site is not None:
            out[drive] = library | site
    return out


def read_site_baselines(conn: Any) -> dict[str, frozenset[str]]:
    """``site_baselines`` of a SharePoint capture store (``conn``: its SQLite connection):
    one grouped read of every current, not removed, drive item's permission list. Raises
    on an unreadable store (the caller decides: nothing opens)."""
    rows = conn.execute(
        "SELECT drive_id, source_acl, count(*) FROM items WHERE superseded_at IS NULL "
        "AND removed_reason IS NULL GROUP BY drive_id, source_acl").fetchall()
    urls: dict[str, Any] = {}
    out = []
    for drive_id, acl_text, count in rows:
        if drive_id not in urls:
            import json
            row = conn.execute("SELECT record FROM items WHERE drive_id = ? AND "
                               "superseded_at IS NULL LIMIT 1", (drive_id,)).fetchone()
            try:
                urls[drive_id] = json.loads(row[0]).get("original_url") if row else None
            except (TypeError, ValueError, AttributeError):
                urls[drive_id] = None
        out.append((drive_id, urls[drive_id], acl_text, count))
    return site_baselines(out)


def inherits_site(record: Any, baselines: Mapping[str, frozenset[str]]) -> bool:
    """Whether a SharePoint file carries its library's and site's inherited permissions,
    or broader (``baselines``: ``site_baselines``): its complete permission list, and the
    item's latest one where recorded (``item_source_acl``), name every baseline
    principal. A file with unique, narrower permissions (its own, or a folder's it
    inherits), an incomplete list or a drive of no baseline: False."""
    if not isinstance(record, Mapping) or not isinstance(record.get("drive_id"), str):
        return False
    baseline = baselines.get(record["drive_id"].strip().lower())
    if not baseline:
        return False
    for field in ("source_acl", "item_source_acl"):
        if field == "item_source_acl" and field not in record:
            continue
        held = principals(record.get(field))
        if held is None or not held >= baseline:
            return False
    return True


_UNITS_LOCK = threading.Lock()
_UNITS: dict[Any, tuple[int, dict[str, Unit]]] = {}


def load_units(reader: Any) -> Mapping[str, Unit] | None:
    """``search.audience_units`` read through a Postgres store reader's own connection
    (``operating.pg.ontology_reader.PgStoreReader``), kept per search write generation
    (one read per generation, not per session). None: no Postgres reader, or the table
    or the generation cannot be read (the caller decides as before)."""
    conn = getattr(reader, "conn", None)
    pg = getattr(conn, "_pg", None)
    if pg is None:
        return None
    from operating.pg.permissions import write_generation
    try:
        with reader.lock, conn._lock:
            generation = write_generation(pg)
            with _UNITS_LOCK:
                hit = _UNITS.get(id(reader))
            if hit is not None and hit[0] == generation:
                return hit[1]
            rows = pg.execute("SELECT audience_key, sharing, people "
                              "FROM search.audience_units").fetchall()
    except Exception:  # noqa: BLE001 - unreadable: no staff scope, decided as before
        try:
            pg.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None
    units = {str(key): Unit(str(sharing or ""),
                            frozenset(str(p).strip().lower() for p in people or ()
                                      if isinstance(p, str) and p.strip()))
             for key, sharing, people in rows}
    with _UNITS_LOCK:
        held = _UNITS.get(id(reader))
        if held is not None and held[1] == units:
            # the same units under a new generation (a search write that changed no unit:
            # new mail's chunks): the same object, so a policy kept over it stays kept
            # (``policy_inputs`` holds it by identity; a new one decided every reader's
            # policy again on every worker after each ingest batch)
            units = held[1]
        _UNITS[id(reader)] = (generation, units)
    return units


_MEETINGS = "meetings:"


def _planner_id(value: str) -> bool:
    """Whether ``value`` is a Planner plan id in the capture's grammar (``TASK_KIND``), so
    a ``meetings:plan:`` key is granted as exactly that plan."""
    try:
        from integrations.capture_meetings import TASK_KIND
        return TASK_KIND.canonical_path((value, value))[0] == value \
            and TASK_KIND.unit((value,)) == f"{_MEETINGS}plan:{value}"
    except (ValueError, TypeError, ImportError):
        return False


def staff_grants(keys: Iterable[str], units: Mapping[str, Unit], *, own: str,
                 mailboxes: Iterable[str] = (),
                 excluded: Callable[[Iterable[str]], bool] | None) -> dict[str, bool]:
    """Pattern -> granted, for the staff reader whose object id is ``own``, over the store's
    audience ``keys``: the business family wildcards, then an exact entry for every key of
    a family this scope decides (``mail``, ``teams``, ``sharepoint``, ``meetings``) and an
    exact denial for a business unit the exclusion takes out. ``mailboxes``: the shared
    mailboxes open to every employee (``shared_mailboxes``). ``excluded``: whether any of
    the given people is excluded; None when the exclusion is not usable (business families
    only, minus any unit that names a person). Keys of other families (``brain``,
    ``mail-any`` ...) are left out: the caller decides them."""
    own = str(own or "").strip().lower()
    if not _GUID.match(own):
        own = ""   # no object id: no personal unit is the reader's
    open_mail = {str(m).strip().lower() for m in mailboxes} | ({own} if own else set())
    out: dict[str, bool] = {f"{family}:*": True for family in BUSINESS_FAMILIES}
    out[ORG_CHART_KEY] = excluded is not None
    for key in sorted({str(k) for k in keys}):
        family = key.partition(":")[0]
        unit = units.get(key)
        people = unit.people if unit is not None else frozenset()
        if family in BUSINESS_FAMILIES:
            if excluded is None:
                if people or (unit is not None and unit.sharing == "personal"):
                    out[key] = False
            elif people and excluded(people):
                out[key] = False
            elif unit is not None and unit.sharing == "personal" and own not in people:
                out[key] = False   # a business unit kept for named people only
            continue
        if family not in ("mail", "teams", "sharepoint") and not key.startswith(_MEETINGS):
            continue
        if excluded is None or "*" in key:
            # no usable exclusion; or a key that would act as a pattern: never granted
            out[key] = False
            continue
        if family == "mail":
            owner = key[len("mail:"):].strip().lower()
            granted = bool(_GUID.match(owner)) and owner in open_mail \
                and not excluded([owner])
        elif key.startswith(_MEETINGS):
            part, _, owner = key[len(_MEETINGS):].partition(":")
            owner = owner.strip().lower()
            if part in ("calendar", "organizer"):
                granted = bool(own) and owner == own and not excluded([owner])
            elif part == "plan":
                granted = _planner_id(owner)
            else:
                granted = False
        elif unit is None:
            granted = False
        elif family == "teams":
            granted = (unit.sharing == "shared" and key.startswith("teams:channel:")) \
                or (bool(own) and own in people)
        else:   # sharepoint: a shared site, or the reader's own OneDrive
            granted = unit.sharing == "shared" or (bool(own) and own in people)
        if granted and people and excluded(people):
            granted = False
        out[key] = granted
    return out
