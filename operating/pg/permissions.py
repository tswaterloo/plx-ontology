"""A reader's ``ReadPolicy`` as the permission filter of every search query (fail closed).

The the rule: every query filters ``audience_key = ANY($allowed_keys) AND
markings <@ $held_markings`` inside SQL, before ranking, and the item gate still runs on the
hits shown. This module turns the existing per-reader policy
(``operating.ontology_tools.ReadPolicy``, built per reader by
``operating.ontology_host.case_host_policy``) into those two arrays (``SearchScope``).

Allowed keys. The policy grants by exact key or trailing-``*`` prefix, most specific
pattern first. That rule is not re-implemented in SQL: the audience keys the search tables
hold are listed (a loose index scan) and each is decided by ``ReadPolicy.may_read`` itself.
A key written after the listing is in no set, so it stays hidden and uncounted: new data
fails closed.

People exclusion (``integrations.people_exclusion``, ``PLX_EXCLUDED_PEOPLE_OIDS`` /
``_FILE``). A unit of an excluded person (a mailbox ``mail:<oid>`` of one, or a unit whose
``search.audience_units.people`` names one) is dropped from results and from hidden
counts. A mail key that is not an object id, or a unit naming a malformed person id, is
dropped too. When the exclusion list does not load, every unit that can hold a person's
own data (mail, all of Teams, SharePoint drives not recorded as shared, personal knowledge
namespaces, any unit naming people, any family not known to be shared) is dropped and no
hidden count is given (``withheld``).

Hidden counts (``hidden_counts``) never title a row. They name an owner only for a shared
unit (a Portal relation family, a BC company, a SharePoint site or list, a Teams team or
channel, a Mission Control scope, a shared knowledge namespace, or a unit the ingest records
as ``shared``), by its human label (never a raw id); or for a unit the reader may open
(the policy grants it; only markings hide the rows). Everything else (other people's mail,
chats, OneDrives, personal namespaces, unknown units) is one unnamed count.

Scopes are cached per (database, write generation, policy, actor, exclusion) for at most
``ScopeCache.ttl`` seconds (``search.write_generation`` is bumped by every write that can
change the keys or units).
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
from typing import Any, Iterable, Mapping, Sequence

import psycopg

from integrations.knowledge_source import is_personal as _personal_namespace
from integrations.people_exclusion import PeopleExclusion, is_object_id
from ontology import store_ddl
from ontology.types import Actor
from operating.ontology_tools import ReadPolicy

SEARCH_TABLES = ("chunks", "entities")
_ALIAS = re.compile(r"^[a-z_][a-z0-9_]*$")
PRIVATE_BUCKET = "Other people's mail, chats and files you can't open"
WITHHELD_EXCLUSION = "people_exclusion_unavailable"
# Families whose units are shared by nature (a units row saying 'personal' still wins).
_SHARED_FAMILIES = {"api": "Portal", "bc": "Business Central", "mc": "Mission Control",
                    "sharepoint-list": "SharePoint"}
# Human fallback labels by family, for a unit the reader may open or a shared unit with
# no recorded label. Never a raw id.
_FAMILY_LABELS = {**_SHARED_FAMILIES, "mail": "Mail", "teams": "Teams",
                  "sharepoint": "SharePoint", "brain": "Knowledge base",
                  "original": "Documents", "repo": "Repository"}


@dataclass(frozen=True)
class Unit:
    """What ``search.audience_units`` records about one audience unit."""
    sharing: str
    label: str | None = None
    people: tuple[str, ...] = ()


def _family(key: str) -> str:
    return key.partition(":")[0]


def is_shared(key: str, unit: Unit | None) -> bool:
    """Whether the unit is shared (its owner label may be shown to anyone). Unknown:
    personal."""
    if unit is not None and unit.sharing == "personal":
        return False
    if unit is not None and unit.sharing == "shared":
        return True
    family = _family(key)
    if family in _SHARED_FAMILIES or key.startswith("teams:channel:"):
        return True
    if family == "brain":
        return not _personal_namespace(key.partition(":")[2])
    return False


def _label(key: str, unit: Unit | None) -> str:
    if unit is not None and unit.label:
        return unit.label
    if key.startswith("teams:chat:"):
        return "Teams chat"
    return _FAMILY_LABELS.get(_family(key), "Other source")


def is_excluded(key: str, unit: Unit | None, exclusion: PeopleExclusion) -> bool:
    """Whether the unit is dropped under the people exclusion (fail closed)."""
    if exclusion.unavailable:
        # Without the list only shared units that name no person survive.
        return (not is_shared(key, unit) or _family(key) in ("mail", "teams")
                or bool(unit and unit.people))
    people = list(unit.people) if unit else []
    if _family(key) == "mail":
        people.append(key.partition(":")[2])
    for person in people:
        if not is_object_id(person) or exclusion.is_excluded(person):
            return True
    return False


@dataclass(frozen=True)
class SearchScope:
    """What one reader may see in the search tables, now.

    ``allowed_audience_keys`` / ``held_markings`` are the SQL filter. ``countable`` maps
    each key whose hidden rows may be counted to its owner label, or to None for the one
    unnamed bucket. ``withheld`` says why no hidden count may be given."""

    allowed_audience_keys: tuple[str, ...]
    held_markings: tuple[str, ...]
    countable: Mapping[str, str | None] = field(default_factory=dict)
    withheld: str | None = None

    def __post_init__(self) -> None:
        keys = tuple(sorted(set(self.allowed_audience_keys)))
        if any(not isinstance(k, str) or not k or "*" in k for k in keys):
            raise ValueError("search_scope_key_invalid")
        held = tuple(sorted(set(self.held_markings)))
        if set(held) - set(store_ddl.MARKINGS):
            raise ValueError("search_scope_marking_unknown")
        object.__setattr__(self, "allowed_audience_keys", keys)
        object.__setattr__(self, "held_markings", held)
        object.__setattr__(self, "countable", dict(self.countable))

    @property
    def empty(self) -> bool:
        return not self.allowed_audience_keys

    def clause(self, alias: str = "c") -> str:
        """The SQL predicate a row of ``alias`` must pass to be visible (named params
        ``plx_allowed_keys`` / ``plx_held_markings``, see ``params``)."""
        if not isinstance(alias, str) or not _ALIAS.match(alias):
            raise ValueError("search_scope_alias_invalid")
        return (f"({alias}.audience_key = ANY(%(plx_allowed_keys)s::text[]) "
                f"AND {alias}.markings <@ %(plx_held_markings)s::text[])")

    def params(self, **extra: Any) -> dict[str, Any]:
        if any(name.startswith("plx_") for name in extra):
            raise ValueError("search_scope_param_clash")
        return dict(extra, plx_allowed_keys=list(self.allowed_audience_keys),
                    plx_held_markings=list(self.held_markings))


EMPTY_SCOPE = SearchScope((), (), withheld="no_read_policy")


def scope_for(policy: Any, actor: Any, audience_keys: Iterable[str], *,
              units: Mapping[str, Unit] | None = None,
              exclusion: PeopleExclusion | None = None) -> SearchScope:
    """The reader's scope over ``audience_keys`` (the keys the tables hold). Fail closed:
    no policy, no actor, another tenant: the empty scope; no exclusion object: an
    unavailable exclusion."""
    if (not isinstance(policy, ReadPolicy) or not isinstance(actor, Actor)
            or actor.tenant != policy.tenant):
        return EMPTY_SCOPE
    if not isinstance(exclusion, PeopleExclusion):
        exclusion = PeopleExclusion(unavailable="excluded_people_oids_absent")
    units = units or {}
    allowed: set[str] = set()
    countable: dict[str, str | None] = {}
    for key in audience_keys:
        if not isinstance(key, str) or not key or "*" in key or ":" not in key:
            continue
        unit = units.get(key)
        if is_excluded(key, unit, exclusion):
            continue
        readable = policy.may_read(actor, key)
        if readable:
            allowed.add(key)
        countable[key] = _label(key, unit) if readable or is_shared(key, unit) else None
    withheld = WITHHELD_EXCLUSION if exclusion.unavailable else None
    return SearchScope(tuple(allowed), tuple(policy.held_markings(actor)),
                       {} if withheld else countable, withheld)


# ---------------------------------------------------------------- database reads

def audience_keys_in(conn: psycopg.Connection,
                     tables: Iterable[str] = SEARCH_TABLES) -> list[str]:
    """Every distinct audience key of the search tables (a loose index scan: one index
    probe per distinct key, not a scan of every row)."""
    keys: set[str] = set()
    for table in tables:
        if table not in SEARCH_TABLES:
            raise ValueError(f"search_table_unknown:{table}")
        keys.update(row[0] for row in conn.execute(
            f"""WITH RECURSIVE k(key) AS (
                    (SELECT audience_key FROM search.{table} ORDER BY audience_key LIMIT 1)
                    UNION ALL
                    SELECT (SELECT t.audience_key FROM search.{table} t
                            WHERE t.audience_key > k.key ORDER BY t.audience_key LIMIT 1)
                    FROM k WHERE k.key IS NOT NULL)
                SELECT key FROM k WHERE key IS NOT NULL"""))
    return sorted(keys)


def units_in(conn: psycopg.Connection) -> dict[str, Unit]:
    return {key: Unit(sharing, label, tuple(people)) for key, sharing, label, people in
            conn.execute("SELECT audience_key, sharing, label, people "
                         "FROM search.audience_units")}


def write_generation(conn: psycopg.Connection) -> int:
    last, called = conn.execute(
        "SELECT last_value, is_called FROM search.write_generation").fetchone()
    return int(last) if called else 0


# ---------------------------------------------------------------- exclusion + cache

_EXCLUSION_LOCK = threading.Lock()
_EXCLUSION: tuple[tuple, PeopleExclusion] | None = None


def load_exclusion(environ: Mapping[str, str] | None = None) -> PeopleExclusion:
    """The people exclusion from the environment, reloaded when its settings or file
    change. Never raises: a broken source is an unavailable guard (fail closed)."""
    global _EXCLUSION
    environ = os.environ if environ is None else environ
    path = environ.get("PLX_EXCLUDED_PEOPLE_FILE") or ""
    try:
        mtime = Path(path).stat().st_mtime_ns if path else None
    except OSError:
        mtime = -1
    stamp = (environ.get("PLX_EXCLUDED_PEOPLE_OIDS"), path, mtime)
    with _EXCLUSION_LOCK:
        if _EXCLUSION is None or _EXCLUSION[0] != stamp:
            try:
                guard = PeopleExclusion.from_env(environ)
            except Exception:  # noqa: BLE001 - any failure is an unavailable guard
                guard = PeopleExclusion(unavailable="excluded_people_file_unreadable")
            _EXCLUSION = (stamp, guard)
        return _EXCLUSION[1]


def _fingerprint(policy: ReadPolicy, actor: Actor) -> str:
    body = {"tenant": policy.tenant,
            "audiences": {p: sorted(v) for p, v in sorted(policy.audiences.items())},
            "markings": {m: sorted(v) for m, v in sorted(policy.markings.items())},
            "units": {f: list(k) for f, k in sorted(policy.units.items())},
            "actor": [actor.tenant, sorted(actor.principals), actor.clearance]}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


class ScopeCache:
    """Scopes per (database, write generation, policy+actor fingerprint, exclusion
    object), each kept at most ``ttl`` seconds; the key and unit listing per (database,
    generation) likewise."""

    def __init__(self, ttl: float = 60.0, maxsize: int = 512) -> None:
        self.ttl, self.maxsize = ttl, maxsize
        self._lock = threading.Lock()
        self._entries: OrderedDict[tuple, tuple[float, Any, Any]] = OrderedDict()
        self.listings = 0  # listings read from the database (observability, tests)

    def _get(self, key: tuple, guard: Any = None) -> Any:
        with self._lock:
            hit = self._entries.get(key)
            if hit is None or time.monotonic() - hit[0] > self.ttl or hit[2] is not guard:
                return None
            self._entries.move_to_end(key)
            return hit[1]

    def _put(self, key: tuple, value: Any, guard: Any = None) -> None:
        with self._lock:
            self._entries[key] = (time.monotonic(), value, guard)
            self._entries.move_to_end(key)
            while len(self._entries) > self.maxsize:
                self._entries.popitem(last=False)

    def scope(self, conn: psycopg.Connection, policy: Any, actor: Any,
              exclusion: PeopleExclusion) -> SearchScope:
        if (not isinstance(policy, ReadPolicy) or not isinstance(actor, Actor)
                or actor.tenant != policy.tenant):
            return EMPTY_SCOPE
        info = conn.info
        db = (info.host, info.port, info.dbname)
        generation = write_generation(conn)
        listing = self._get(("listing", db, generation))
        if listing is None:
            listing = (audience_keys_in(conn), units_in(conn))
            self.listings += 1
            self._put(("listing", db, generation), listing)
        key = ("scope", db, generation, _fingerprint(policy, actor))
        scope = self._get(key, exclusion)
        if scope is None:
            scope = scope_for(policy, actor, listing[0], units=listing[1], exclusion=exclusion)
            self._put(key, scope, exclusion)
        return scope


DEFAULT_CACHE = ScopeCache()


def reader_scope(conn: psycopg.Connection, policy: Any, actor: Any, *,
                 exclusion: PeopleExclusion | None = None,
                 cache: ScopeCache | None = DEFAULT_CACHE) -> SearchScope:
    """The reader's scope over what the search tables hold now (cached, see
    ``ScopeCache``). ``exclusion`` defaults to ``load_exclusion()``."""
    exclusion = load_exclusion() if exclusion is None else exclusion
    if cache is None:
        return scope_for(policy, actor, audience_keys_in(conn), units=units_in(conn),
                         exclusion=exclusion)
    return cache.scope(conn, policy, actor, exclusion)


# ---------------------------------------------------------------- hidden counts

def hidden_counts(conn: psycopg.Connection, scope: SearchScope, *,
                  text: str | None = None, linked_ids: Sequence[str] | None = None,
                  kinds: Sequence[str] | None = None, object_ids: Sequence[str] | None = None,
                  table: str = "chunks") -> dict[str, Any]:
    """How many rows matching the given filters (all bound parameters: a websearch text
    query over ``tsv``, any of ``linked_ids``, ``kinds``, ``object_ids``) the reader may
    not see, in total and by owner or team, per the rules of this module. Counts only.
    ``count`` is None when the count is withheld (``withheld`` says why)."""
    if table not in SEARCH_TABLES:
        raise ValueError(f"search_table_unknown:{table}")
    if scope.withheld:
        return {"count": None, "by_owner": [], "withheld": scope.withheld}
    where = ["c.audience_key = ANY(%(plx_countable_keys)s::text[])",
             f"({scope.clause('c')}) IS NOT TRUE"]
    params: dict[str, Any] = {"plx_countable_keys": sorted(scope.countable)}
    if text is not None:
        if table != "chunks":
            raise ValueError("hidden_counts_text_needs_chunks")
        where.append("c.tsv @@ websearch_to_tsquery('english', %(plx_text)s)")
        params["plx_text"] = str(text)
    for name, values, column in (("linked", linked_ids, "linked_ids"),
                                 ("kinds", kinds, "kind"), ("objects", object_ids, "object_id")):
        if values is None:
            continue
        if isinstance(values, str) or not all(isinstance(v, str) for v in values):
            raise ValueError(f"hidden_counts_{name}_invalid")
        if column != "object_id" and table != "chunks":
            raise ValueError(f"hidden_counts_{name}_needs_chunks")
        where.append({"linked_ids": "c.linked_ids && %(plx_linked)s::text[]",
                      "kind": "c.kind = ANY(%(plx_kinds)s::text[])",
                      "object_id": "c.object_id = ANY(%(plx_objects)s::text[])"}[column])
        params[f"plx_{name}"] = list(values)
    rows = conn.execute(
        f"SELECT c.audience_key, count(*) FROM search.{table} c WHERE "
        + " AND ".join(where) + " GROUP BY c.audience_key",
        {**params, "plx_allowed_keys": list(scope.allowed_audience_keys),
         "plx_held_markings": list(scope.held_markings)}).fetchall()
    named: dict[str, int] = {}
    unnamed = 0
    for key, count in rows:
        label = scope.countable.get(key)
        if label is None:
            unnamed += count
        else:
            named[label] = named.get(label, 0) + count
    by_owner = [{"owner_or_team": label, "count": count, "named": True}
                for label, count in sorted(named.items(), key=lambda i: (-i[1], i[0]))]
    if unnamed:
        by_owner.append({"owner_or_team": PRIVATE_BUCKET, "count": unnamed, "named": False})
    return {"count": sum(named.values()) + unnamed, "by_owner": by_owner}
