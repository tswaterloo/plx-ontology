"""Row 14: identifiers a mail writes bare (a customer code ``ABCD01``, an item code
``AB1234``, a 10-digit order number) as lead mention links, kept beside the served links in
``ontology.bare_mentions`` (migration 0035) and read only through a reader switch.

Why beside, not in ``link_rows``: ``ontology/mentions.py`` (the ``mentions`` step) is
digest-pinned, and the steps after it re-judge from the live mention leads
(``sender_parties``, ``sender_corroboration``, ``mentions`` ``party_domain``). A row written
into ``link_rows`` by code outside the steps would change their inputs without moving a
digest. Here the build steps never read the table (their search_path is ``ontology,
public``), ``link_rows`` is never written, and no schema, step or key digest moves.

Which tokens link (a closed vocabulary, no shape guess):

* a whitespace/punctuation-delimited token of 5..40 characters that holds a digit, outside
  URLs and mail addresses; letters and digits both, or digits only when 8 or more (years,
  quantities, prices and short numbers never link); not a date or a dash-joined number run;
* its letters are not one of the own-party words (``document_fields.json`` ``own_parties``:
  a vendor's account code for PLX itself names no customer of ours);
* its upper-cased form equals the ``normalized`` value of a current served identifier of one
  of :data:`KINDS`, held by an object of that kind's target type. One holder: a lead to it.
  Several holders (at most :data:`MAX_HOLDERS`, all of one type) link when they are one
  record: joined by ``same_record`` links at a confirming tier (:data:`TWIN_TIERS`, the
  tiers ``sender_corroboration`` follows twins by), or at most one holder per source system
  (:func:`systems`: a customer code each BC company, the Portal and Sage hold once).
  Otherwise the token is ambiguous (two customers of one system sharing a code, a customer
  code that is also an item code) and links nothing;
* a token, a dash and a suffix that together write a longer known code (``AC600 - NF`` for
  ``AC600-NF``) link the longer code only, never the shorter one; a suffix of digits a
  quantity word follows is a quantity, never joined (``MC4015 - 2 cases`` names MC4015, not
  the item MC4015-2: :data:`QUANTITY_WORDS`);
* a (mail object, target) pair the served ``Email.mentions_*`` links already hold is skipped.

Each row is a lead (``tier`` ``lead``, ``binding_kind`` ``mention``), cites the mail's own
L1 handle and carries the mail object's current markings, so every consumer that filters
``links`` by markings filters these alike. A mail of an excluded person is never read (the
exclusion not loaded refuses: fail closed).

Reading: ``PLX_PG_BARE_MENTIONS=1`` puts ``ontology_bare`` (or, with
``PLX_PG_MENTION_REPEATS=1`` too, ``ontology_bare_seen``) first on a reader's search_path
(:func:`search_path`); its ``links`` view is the served links plus these rows. Off, nothing
reads the table.

    python3 -m operating.pg.bare_mentions propose --handles FILE   # read-only, JSON lines
    python3 -m operating.pg.bare_mentions build [--workers N] [--limit N] [--if-migrated]
    python3 -m operating.pg.bare_mentions clear                     # the undo (switch off first)
    python3 -m operating.pg.bare_mentions count

``build`` is safe with the switch on and is also the nightly rebuild (``plx-bare-mentions``):
it links every current mail into a staging table (:data:`NEXT`), then in one transaction
replaces the served rows by the staged ones (a reader sees the old rows or the new, never an
empty or half table). A delta ``refresh`` committed while it ran is logged (:data:`PENDING`)
and run again after the merge, so a change the staged ranges read too early is not lost.

``refresh`` runs once per delta cycle, after the cycle's facts are kept: SQL rechecks of the
rows of the records the change feed says changed (no mail read for a customer, item or
order), and only the changed mails read again, bounded (:data:`MAX_MAILS`). The nightly run
closes what it leaves: a mail naming a record by an identifier new to that record, a mail
over the bound, a served link that went away.

Stdlib and psycopg.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
from pathlib import Path
import re
import sys
import time
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from ontology import parent_watch
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
ENV = "PLX_PG_BARE_MENTIONS"
TABLE = "ontology.bare_mentions"
NEXT = "ontology.bare_mentions_next"         # the rows a build stages, merged at its end
PENDING = "ontology.bare_mentions_pending"   # objects a refresh changed while a build ran
# advisory locks: one build at a time; a merge and a refresh never interleave
BUILD_LOCK = int.from_bytes(hashlib.sha256(b"bare_mentions:build").digest()[:8], "big",
                            signed=True)
MERGE_LOCK = int.from_bytes(hashlib.sha256(b"bare_mentions:merge").digest()[:8], "big",
                            signed=True)
ID_PREFIX = "bare:"
# identifier kind -> (target object type, the schema's link type from Email to it)
KINDS: dict[str, tuple[str, str]] = {
    "customer_erp_code": ("Customer", "Email.mentions_customer"),
    "erp_item_code": ("Product", "Email.mentions_product"),
    "item_code": ("Product", "Email.mentions_product"),
    "so_number": ("SalesOrder", "Email.mentions_sales_order"),
}
MAX_HOLDERS = 8
# a delta cycle's refresh (:func:`refresh`) is bounded: mails read again, seconds of
# reading, changed customers/items/orders rechecked, seconds per SQL statement; what is
# over waits for the nightly build
MAX_MAILS, MAIL_SECONDS, MAX_TARGETS, STATEMENT_SECONDS = 2000, 10.0, 20000, 30
# tiers at which a ``same_record`` link makes two holders one record (sender_corroboration)
TWIN_TIERS = ("verified", "reviewed", "corroborated")
MIN_CHARS, MAX_CHARS, MIN_DIGITS_ONLY = 5, 40, 8
REASON = json.dumps([
    {"agrees": True, "feature": "bare_identifier",
     "detail": "a token equal to a served identifier of the target's kind "
               "(operating/pg/bare_mentions.py)"},
    {"agrees": None, "feature": "uncorroborated",
     "detail": "one feature only: a lead, never more"}], sort_keys=True)
COLUMNS = ("link_id", "link_type", "from_object", "to_object", "tier", "binding_kind",
           "link_contract", "reason", "why_not", "evidence_handles", "reviewed_by",
           "reviewed_at", "review_action_id", "markings", "recorded_at", "superseded_at")

_TOKEN = re.compile(r"(?<![A-Za-z0-9@._/\\-])([A-Za-z0-9][A-Za-z0-9-]*[A-Za-z0-9])"
                    r"(?![A-Za-z0-9@_/\\]|[.-][A-Za-z0-9])")
_URL_OR_ADDRESS = re.compile(r"(?i)\b(?:https?://|www\.)\S+|\S+@\S+|<[^>\s]*[/@][^>\s]*>")
_NUMBER_RUN = re.compile(r"\d+(?:-\d+)+")
# a token, a dash (hyphen, en or em dash) with up to two spaces or tabs around it, a suffix:
# "AC600 \u2013 NF" may write the known code AC600-NF
_DASH_JOIN = re.compile(r"[ \t]{0,2}[-\u2010\u2011\u2013\u2014][ \t]{0,2}([A-Za-z0-9]{1,12})"
                        r"(?![A-Za-z0-9@_/\\]|[.-][A-Za-z0-9])")
ALT = "|"   # "LONGER|SHORT..." : the longer code when it is a known one, else the short parts
# a word after "MC4015 - 2" that makes the digits a quantity ("2 cases"), never a code's suffix
QUANTITY_WORDS = frozenset((
    "bag bags box boxes bx bundle bundles can cans carton cartons case cases cs ctn ctns doz "
    "dozen drum drums ea each gal gallon gallons kg kgs kit kits lb lbs pack packs pail pails "
    "pair pairs pallet pallets pc pcs piece pieces pk pkg pkgs plt plts qty roll rolls set sets "
    "skid skids tote totes unit units x").split())
_NEXT_WORD = re.compile(r"[ \t]{0,3}([A-Za-z]+)\b")


def switch_on(env: Mapping[str, str] | None = None) -> bool:
    return (os.environ if env is None else env).get(ENV) == "1"


def search_path(env: Mapping[str, str] | None = None) -> str:
    """A Postgres reader's search_path for both reader switches (this one and the mention
    repeats', ``operating.pg.mention_repeats``)."""
    from operating.pg import mention_repeats
    seen = mention_repeats.switch_on(env)
    if not switch_on(env):
        return mention_repeats.SEEN_PATH if seen else mention_repeats.PLAIN_PATH
    return "ontology_bare_seen, ontology, public" if seen else "ontology_bare, ontology, public"


def own_words(root: Path = ROOT) -> frozenset[str]:
    data = json.loads((root / "ontology/schema/document_fields.json").read_text())
    names = data["own_parties"]["names"]
    return frozenset(w.lower() for n in names if "." not in n for w in n.split())


def plain(text: str) -> str:
    if "<" in text and re.search(r"</?(html|body|div|p|br|table|span)\b", text, re.I):
        text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", text)
        text = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|td)>", "\n", text)
        text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    return text


def _shaped(v: str, own: frozenset[str]) -> bool:
    if not MIN_CHARS <= len(v) <= MAX_CHARS or not any(c.isdigit() for c in v):
        return False
    if v.isdigit():
        if len(v) < MIN_DIGITS_ONLY:
            return False
    elif not any(c.isalpha() for c in v) or _NUMBER_RUN.fullmatch(v):
        return False
    return re.sub(r"[^A-Z]", "", v).lower() not in own


def candidates(text: str, own: Iterable[str]) -> set[str]:
    """The upper-cased tokens of ``text`` that may name a record (module docstring).

    A token followed by a dash and a suffix (``AC600 \u2013 NF``) is one alternative
    ``"AC600-NF|AC600"`` (:data:`ALT`): :func:`mail_rows` links the longer code when it is a
    known identifier and never the shorter one; else the shorter parts as plain tokens."""
    own = frozenset(own)
    out: set[str] = set()
    clean = _URL_OR_ADDRESS.sub(" ", text)
    skip_to = -1
    for m in _TOKEN.finditer(clean):
        if m.start() < skip_to:     # the suffix of a dash join already taken
            continue
        v = m.group(1).upper()
        j = _DASH_JOIN.match(clean, m.end())
        if j and j.group(1).isdigit():
            # "MC4015 - 2 cases": a digit suffix a quantity word follows is a quantity
            word = _NEXT_WORD.match(clean, j.end())
            if word and word.group(1).lower() in QUANTITY_WORDS:
                j = None
        if j:
            longer = f"{v}-{j.group(1).upper()}"
            suffix = j.group(1).upper()
            if _shaped(longer, own):
                short = [x for x in (v, suffix) if _shaped(x, own)]
                out.add(ALT.join([longer, *short]))
                skip_to = j.end()
                continue
        if _shaped(v, own):
            out.add(v)
    return out


def _alternatives(token: str) -> tuple[str, list[str]]:
    longer, *short = token.split(ALT)
    return longer, short


def check_schema(link_types: Mapping[str, Mapping[str, Any]]) -> None:
    """Every link type :data:`KINDS` writes is the schema's, from Email to that target;
    otherwise refuse (fail closed)."""
    for kind, (target, link_type) in KINDS.items():
        lt = link_types.get(link_type)
        if not lt or lt.get("from") != "Email" or lt.get("to") != target:
            raise SystemExit(f"bare_mentions: {link_type} is not Email->{target} in the "
                             f"served schema ({kind}); refused")


def twin_groups(conn: Any, objects: Iterable[str]) -> dict[str, str]:
    """object -> its root among ``objects`` joined by current ``same_record`` links at a
    :data:`TWIN_TIERS` tier (one hop through any object, transitively). An object no such
    link touches is its own root."""
    objects = sorted(set(objects))
    parent: dict[str, str] = {o: o for o in objects}

    def root(x: str) -> str:
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    if len(objects) > 1:
        for a, b in conn.execute(
                "SELECT from_object, to_object FROM ontology.link_rows WHERE binding_kind = "
                "'same_record' AND tier = ANY(%s) AND superseded_at IS NULL AND "
                "(from_object = ANY(%s) OR to_object = ANY(%s))",
                (list(TWIN_TIERS), objects, objects)).fetchall():
            ra, rb = root(a), root(b)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)
    return {o: root(o) for o in objects}


def systems(obj: str, lineage: str | None, handle: str | None) -> set[str]:
    """The source-system keys of a holder: its identifier's ``lineage`` (``bc_distribution``,
    ``sage_bronze``, ``gold_etl_from_fm``...) and its object binding (``bc.customers``,
    ``portal.app_customer``), the binding of a Business Central record qualified by its
    company (``bc://<environment>/<company>/...``). Two holders sharing any key are in one
    system. No lineage: the key ``"?"``, which :func:`resolve` treats as every system."""
    binding = obj.split(":", 2)[1] if obj.count(":") >= 2 else obj
    if handle and handle.startswith("bc://"):
        binding += "@" + "/".join(handle[len("bc://"):].split("/")[:2])
    return {"lineage:" + lineage if lineage else "?", "binding:" + binding}


def _one_per_system(holders: set[str], group: Mapping[str, str],
                    keys: Mapping[str, set[str]]) -> bool:
    """True when no two holders of different confirmed twin groups share a system key."""
    owner: dict[str, str] = {}
    groups = {group.get(o, o) for o in holders}
    for o in holders:
        g = group.get(o, o)
        if "?" in keys.get(o, {"?"}) and len(groups) > 1:
            return False
        for k in keys.get(o, ()):
            if owner.setdefault(k, g) != g:
                return False
    return True


def resolve(conn: Any, tokens: Iterable[str], stats: dict[str, int] | None = None,
            known: set[str] | None = None) -> dict[str, list[tuple[str, str]]]:
    """token -> sorted [(link type, target object)] of its current holders of the right
    type. Several holders of one type link when they are one record (confirmed twins,
    :func:`twin_groups`) or at most one per source system (:func:`systems`): a code a BC
    company, the other BC company, the Portal and Sage each hold once. Absent (counted in
    ``stats["ambiguous"]``): more than :data:`MAX_HOLDERS`, holders of two types (a customer
    code that is also an item code), or two holders in one system that are not twins.
    ``known`` collects every token some holder of the right type holds, linked or not."""
    tokens = sorted(set(tokens))
    if not tokens:
        return {}
    held: dict[str, set[tuple[str, str]]] = defaultdict(set)
    keys: dict[str, set[str]] = defaultdict(set)
    for kind, value, obj, lineage, handle in conn.execute(
            "SELECT identifier_kind, normalized, object_id, lineage, l1_handle "
            "FROM ontology.identifiers WHERE normalized = ANY(%s) AND identifier_kind = "
            "ANY(%s) AND superseded_at IS NULL", (tokens, sorted(KINDS))).fetchall():
        target, link_type = KINDS.get(kind, (None, None))
        if target is not None and obj.split(":", 1)[0] == target:
            held[value].add((link_type, obj))
            keys[obj] |= systems(obj, lineage, handle)
    if known is not None:
        known.update(held)
    several = {v: h for v, h in held.items() if 1 < len(h) <= MAX_HOLDERS}
    group = twin_groups(conn, {o for h in several.values() for _, o in h})
    out: dict[str, list[tuple[str, str]]] = {}
    for v, h in held.items():
        objs = {o for _, o in h}
        if len(h) > MAX_HOLDERS or (v in several and len({group[o] for o in objs}) != 1 and (
                len({t for t, _ in h}) != 1 or not _one_per_system(objs, group, keys))):
            if stats is not None:
                stats["ambiguous"] = stats.get("ambiguous", 0) + 1
            continue
        out[v] = sorted(h)
    return out


def link_id(link_type: str, mail_object: str, target: str) -> str:
    raw = json.dumps([link_type, mail_object, target])
    return ID_PREFIX + hashlib.sha256(raw.encode()).hexdigest()[:32]


def mail_rows(conn: Any, mails: Sequence[Mapping[str, Any]], own: Iterable[str],
              recorded_at: str, stats: dict[str, int] | None = None) -> list[dict[str, Any]]:
    """The rows for ``mails`` (each ``handle`` and ``text``, or ``tokens``: the
    :func:`candidates` of its text, computed where the mail store is): one per (mail
    object, target) not already a served ``Email.mentions_*`` link of that mail object."""
    own = frozenset(own)
    handles = sorted({m["handle"] for m in mails})
    objects: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for obj, handle, markings in conn.execute(
            "SELECT object_id, l1_handle, markings FROM ontology.object_versions "
            "WHERE l1_handle = ANY(%s) AND superseded_at IS NULL", (handles,)).fetchall():
        if obj.startswith("Email:"):
            objects[handle].append((obj, markings))
    tokens = {m["handle"]: (set(m["tokens"]) if "tokens" in m else candidates(m["text"], own))
              for m in mails if objects.get(m["handle"])}
    # an alternative "LONGER|SHORT..." becomes the longer code when it is a known one
    alts = {t for toks in tokens.values() for t in toks if ALT in t}
    known: set[str] = set()
    plain_toks = {t for toks in tokens.values() for t in toks if ALT not in t}
    wanted = plain_toks | {p for t in alts for p in t.split(ALT)}
    holders = resolve(conn, wanted, stats, known)
    chosen = {t: ([_alternatives(t)[0]] if _alternatives(t)[0] in known
                  else _alternatives(t)[1]) for t in alts}
    tokens = {h: {x for t in toks for x in chosen.get(t, [t])} for h, toks in tokens.items()}
    froms = sorted({o for h in tokens for o, _ in objects[h]})
    served = {(f, t) for f, t in conn.execute(
        "SELECT from_object, to_object FROM ontology.link_rows WHERE from_object = ANY(%s) "
        "AND link_type LIKE 'Email.mentions%%' AND superseded_at IS NULL",
        (froms,)).fetchall()} if froms else set()
    rows: dict[str, dict[str, Any]] = {}
    for handle, toks in tokens.items():
        for obj, markings in objects[handle]:
            for tok in sorted(toks):
                for link_type, target in holders.get(tok, ()):
                    lid = link_id(link_type, obj, target)
                    if (obj, target) in served or lid in rows:
                        continue
                    rows[lid] = {
                        "link_id": lid, "link_type": link_type, "from_object": obj,
                        "to_object": target, "tier": "lead", "binding_kind": "mention",
                        "link_contract": None, "reason": REASON,
                        "why_not": json.dumps([{"feature": "bare_identifier", "value": tok}]),
                        "evidence_handles": json.dumps([handle]), "reviewed_by": None,
                        "reviewed_at": None, "review_action_id": None,
                        "markings": markings, "recorded_at": recorded_at,
                        "superseded_at": None}
    return [rows[k] for k in sorted(rows)]


# ------------------------------------------------------------------ the mail store side

def _exclusion() -> Any:
    from integrations.people_exclusion import PeopleExclusion, environ_with_exclusion
    excl = PeopleExclusion.from_env(environ_with_exclusion())
    if excl.unavailable:
        raise SystemExit("bare_mentions: people exclusion not loaded; refused (fail closed)")
    return excl


def read_mails(store: Any, excl: Any, where: str, params: Sequence[Any]
               ) -> list[dict[str, Any]]:
    """Current, kept messages of ``where`` as ``{handle, text}``; an excluded person's
    mailbox is never read; an unreadable one is skipped."""
    out = []
    for row in store.db.execute(
            "SELECT * FROM messages WHERE superseded_at IS NULL AND removed_reason IS NULL "
            f"AND ({where})", tuple(params)).fetchall():
        if excl.is_excluded(row["mailbox_id"]):
            continue
        try:
            record = json.loads(row["record"])
            text = str(record.get("subject") or "") + "\n" + plain(store.body_text(row) or "")
        except Exception:   # unreadable: never linked
            continue
        out.append({"handle": row["l1_handle"], "text": text})
    return out


def _dsn() -> str:
    from operating.pg import config
    return config.dsn()


def _connect(read_only: bool) -> Any:
    import psycopg
    conn = psycopg.connect(_dsn(), autocommit=True, application_name="bare-mentions")
    if read_only:
        conn.execute("SET default_transaction_read_only = on")
    return conn


def _open_store(state: str | None) -> Any:
    from integrations.capture_mail import MailCaptureStore
    return MailCaptureStore.open_read_only(state or os.environ.get("PLX_STATE_DIR",
                                                                   "/data/plx-state"))


def _build_range(args: tuple[int, int, str | None, str]) -> int:
    lo, hi, state, recorded_at = args
    store, conn = _open_store(state), _connect(read_only=False)
    try:
        mails = read_mails(store, _exclusion(), "version_id >= ? AND version_id < ?", (lo, hi))
        rows = mail_rows(conn, mails, own_words(), recorded_at)
        if rows:
            with conn.cursor() as cur:
                cur.executemany(
                    f"INSERT INTO {NEXT} ({', '.join(COLUMNS)}) VALUES "
                    f"({', '.join(['%s'] * len(COLUMNS))}) ON CONFLICT (link_id) DO NOTHING",
                    [tuple(r[c] for c in COLUMNS) for r in rows])
        return len(rows)
    finally:
        conn.close()
        store.db.close()


def table_exists(conn: Any) -> bool:
    return conn.execute("SELECT to_regclass(%s)", (TABLE,)).fetchone()[0] is not None


def build(workers: int, state: str | None, limit: int | None = None, step: int = 2000
          ) -> dict[str, int]:
    """Link every current mail into :data:`NEXT`, version ranges side by side, then
    :func:`merge` them in. One build at a time (refused while another holds the lock). A
    ``limit`` (the first N versions) is a trial: refused once the table holds rows, which
    it would otherwise drop."""
    from ontology.schema import load_schema
    check_schema(load_schema().link_types)
    _exclusion()
    store = _open_store(state)
    lo, hi = store.db.execute("SELECT min(version_id), max(version_id) FROM messages").fetchone()
    store.db.close()
    conn = _connect(read_only=False)
    try:
        if not conn.execute("SELECT pg_try_advisory_lock(%s)", (BUILD_LOCK,)).fetchone()[0]:
            raise SystemExit("bare_mentions: another build is running; refused")
        if limit is not None and conn.execute(
                f"SELECT EXISTS (SELECT 1 FROM {TABLE})").fetchone()[0]:
            raise SystemExit("bare_mentions: a --limit trial on a built table would drop "
                             "its other rows; refused (clear first)")
        conn.execute(f"DROP TABLE IF EXISTS {NEXT}, {PENDING}")   # a crashed run's leftovers
        conn.execute(f"CREATE UNLOGGED TABLE {PENDING} (object_id text NOT NULL)")
        conn.execute(f"CREATE UNLOGGED TABLE {NEXT} (LIKE {TABLE} INCLUDING ALL)")
        staged = 0
        if lo is not None:
            if limit is not None:
                hi = min(hi, lo + limit - 1)
            recorded_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            ranges = [(a, min(a + step, hi + 1), state, recorded_at)
                      for a in range(lo, hi + 1, step)]
            if workers <= 1:
                staged = sum(map(_build_range, ranges))
            else:
                with ProcessPoolExecutor(max_workers=workers,
                                         **parent_watch.pool_init()) as pool:
                    staged = sum(pool.map(_build_range, ranges))
        report = merge(conn)
        report["staged"] = staged
        if report["pending"]:
            report["refreshed"] = refresh(_dsn(), report["pending"], state, max_mails=None,
                                          mail_seconds=None)
        report["pending"] = len(report["pending"])
        report["rows"] = conn.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0]
        return report
    finally:
        try:
            conn.execute(f"DROP TABLE IF EXISTS {NEXT}, {PENDING}")
            conn.execute("SELECT pg_advisory_unlock_all()")
        finally:
            conn.close()


def merge(conn: Any) -> dict[str, Any]:
    """In one transaction: drop the served rows the staged set no longer holds, add the new
    ones, update the ones whose markings or evidence moved (a row kept keeps its
    ``recorded_at``: when it was first linked), and take the objects a refresh logged
    while the build ran (:data:`PENDING`, returned for a refresh after this commit)."""
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (MERGE_LOCK,))
        removed = conn.execute(
            f"DELETE FROM {TABLE} m WHERE NOT EXISTS (SELECT 1 FROM {NEXT} n "
            "WHERE n.link_id = m.link_id)").rowcount
        cols = ", ".join(COLUMNS)
        moved = ("link_type", "from_object", "to_object", "why_not", "evidence_handles",
                 "markings", "reason")
        upserted = conn.execute(
            f"INSERT INTO {TABLE} ({cols}) SELECT {cols} FROM {NEXT} "
            "ON CONFLICT (link_id) DO UPDATE SET "
            + ", ".join(f"{c} = EXCLUDED.{c}" for c in moved)
            + f" WHERE ({', '.join(f'{TABLE}.{c}' for c in moved)}) IS DISTINCT FROM "
            f"({', '.join(f'EXCLUDED.{c}' for c in moved)})").rowcount
        pending = sorted({r[0] for r in conn.execute(
            f"SELECT object_id FROM {PENDING}").fetchall()})
        conn.execute(f"DROP TABLE {NEXT}, {PENDING}")
    return {"removed": removed, "added_or_updated": upserted, "pending": pending}


def refresh(dsn: str, objects: Sequence[str] = (), state: str | None = None, *,
            since_cycle: int | None = None, max_mails: int | None = MAX_MAILS,
            mail_seconds: float | None = MAIL_SECONDS) -> dict[str, Any]:
    """Keep the rows true to records that changed, cheaply: once per delta cycle, after
    everything readers wait on (``operating.pg.ontology_delta.refresh_bare_cycle``).

    The changed records are the change feed's (``ontology.change_feed``, migration 0026)
    past ``since_cycle``: ``n``/``c``/``r`` a record that itself changed, ``l`` one whose
    links only moved. Without ``since_cycle``, ``objects`` (all taken as changed: the
    pending ones a build logged, a layer's owned records when there is no feed). Then:

    * a changed mail (``Email``): its rows whose mail version is no longer current go, the
      others take its current markings; up to ``max_mails`` of them (and ``mail_seconds``
      of reading) are read again from the mail store and their rows replaced, the rest wait
      for the nightly ``build`` (counted in ``mails_deferred``);
    * a mail whose served links moved (``l``): its rows for a pair a served
      ``Email.mentions_*`` link now holds go (both ends of a moved link are in the feed);
    * a changed customer, item or order (at most :data:`MAX_TARGETS`): no mail is read. The
      stored token of each row naming it, and every token it now holds that a row names, is
      resolved again (:func:`resolve`, SQL only); a row whose target no longer holds its
      token, or whose token turned ambiguous, goes. A mail naming a record by an identifier
      new to it is linked by the next ``build`` (nightly), never here.

    Skipped when the table is absent or was never built (an empty table: a first build is
    running and its merge refreshes what was logged). While a build stages, the changed
    records are logged (:data:`PENDING`) for a refresh after its merge."""
    import psycopg
    t0 = time.monotonic()
    report: dict[str, Any] = {"objects": 0}
    with psycopg.connect(dsn, autocommit=True, application_name="bare-mentions") as conn:
        if conn.execute("SELECT to_regclass(%s)", (TABLE,)).fetchone()[0] is None:
            return {**report, "skipped": "no_table"}
        conn.execute(f"SET statement_timeout = '{STATEMENT_SECONDS}s'")
        conn.execute("SET jit = off")
        conn.execute("CREATE TEMP TABLE bare_changed (object_id text PRIMARY KEY, "
                     "kind text NOT NULL)")
        types = sorted({"Email", *(t for t, _ in KINDS.values())})
        if since_cycle is None:
            wanted = sorted({o for o in objects if o.split(":", 1)[0] in types})
            with conn.cursor() as cur, cur.copy(
                    "COPY bare_changed (object_id, kind) FROM STDIN") as copy:
                for o in wanted:
                    copy.write_row((o, "c"))
        else:
            conn.execute(
                "INSERT INTO bare_changed SELECT object_id, CASE WHEN bool_or(kind <> 'l') "
                "THEN 'c' ELSE 'l' END FROM ontology.change_feed WHERE cycle_id > %s AND "
                "kind IN ('n', 'c', 'r', 'l') AND split_part(object_id, ':', 1) = ANY(%s) "
                "GROUP BY object_id", (since_cycle, types))
        conn.execute("ANALYZE bare_changed")
        counts = dict(conn.execute(
            "SELECT split_part(object_id, ':', 1) || ':' || kind, count(*) FROM bare_changed "
            "GROUP BY 1").fetchall())
        report["objects"] = sum(counts.values())
        report["changed"] = dict(sorted(counts.items()))
        if not report["objects"]:
            return {**report, "seconds": round(time.monotonic() - t0, 2)}
        if conn.execute(f"SELECT NOT EXISTS (SELECT 1 FROM {TABLE})").fetchone()[0]:
            with conn.transaction():   # a first build running: its merge refreshes these
                logged = _log_pending(conn)
            return {**report, "skipped": "not_built", "pending": logged}
        # the changed mails to read again: their current versions, by object
        mail_handles: dict[str, list[str]] = defaultdict(list)
        for obj, handle in conn.execute(
                "SELECT c.object_id, v.l1_handle FROM bare_changed c JOIN "
                "ontology.object_versions v ON v.object_id = c.object_id AND "
                "v.superseded_at IS NULL WHERE c.kind = 'c' AND c.object_id LIKE 'Email:%%' "
                "ORDER BY 1, 2").fetchall():
            mail_handles[obj].append(handle)
        mails, read = _read_changed(mail_handles, state, max_mails, mail_seconds)
        report["mails_read"] = len(read)
        report["mails_deferred"] = len(mail_handles) - len(read)
        stats: dict[str, int] = {}
        rows = [r for r in mail_rows(conn, mails, own_words(), _now(), stats)
                if r["from_object"] in read] if mails else []
        with conn.transaction(), conn.cursor() as cur:
            report["pending"] = _log_pending(conn)
            report.update(_recheck_mails(cur))
            report.update(_recheck_targets(conn, cur, stats))
            if read:
                cur.execute(f"DELETE FROM {TABLE} WHERE from_object = ANY(%s)", (sorted(read),))
                report["replaced"] = cur.rowcount
            if rows:
                cur.executemany(
                    f"INSERT INTO {TABLE} ({', '.join(COLUMNS)}) VALUES "
                    f"({', '.join(['%s'] * len(COLUMNS))}) ON CONFLICT (link_id) DO NOTHING",
                    [tuple(r[c] for c in COLUMNS) for r in rows])
        report["rows"] = len(rows)
        report["ambiguous"] = stats.get("ambiguous", 0)
    report["seconds"] = round(time.monotonic() - t0, 2)
    return report


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _read_changed(mail_handles: Mapping[str, Sequence[str]], state: str | None,
                  max_mails: int | None, mail_seconds: float | None
                  ) -> tuple[list[dict[str, Any]], set[str]]:
    """The changed mails' texts, at most ``max_mails`` objects and ``mail_seconds`` of
    reading (the rest wait for the nightly build); the objects read (an excluded or
    unreadable one counts as read: its rows go). The exclusion is loaded first: not
    loaded, nothing is read and nothing written (fail closed)."""
    objs = sorted(mail_handles)[:max_mails] if max_mails is not None else sorted(mail_handles)
    if not objs:
        return [], set()
    excl = _exclusion()
    started = time.monotonic()
    mails: list[dict[str, Any]] = []
    read: set[str] = set()
    store = _open_store(state)
    try:
        for i in range(0, len(objs), 200):
            if mail_seconds is not None and read and time.monotonic() - started > mail_seconds:
                break
            part = objs[i:i + 200]
            handles = sorted({h for o in part for h in mail_handles[o]})
            mails += read_mails(store, excl,
                                f"l1_handle IN ({', '.join('?' * len(handles))})", handles)
            read.update(part)
    finally:
        store.db.close()
    return mails, read


def _token_sql(alias: str = "b") -> str:
    """A row's token in SQL (the expression of the index ``bare_mentions_token``, 0035)."""
    return f"({alias}.why_not::jsonb -> 0 ->> 'value')"


def _recheck_mails(cur: Any) -> dict[str, int]:
    """SQL only, over ``bare_changed``'s mails: the rows of a mail version no longer current
    go, the rest take its current markings; a pair the served mention links now hold goes."""
    cur.execute(
        f"DELETE FROM {TABLE} b USING bare_changed c WHERE c.kind = 'c' AND "
        "b.from_object = c.object_id AND NOT EXISTS (SELECT 1 FROM ontology.object_versions v "
        "WHERE v.object_id = b.from_object AND v.l1_handle = b.evidence_handles::jsonb ->> 0 "
        "AND v.superseded_at IS NULL)")
    gone = cur.rowcount
    cur.execute(
        f"UPDATE {TABLE} b SET markings = v.markings FROM bare_changed c, "
        "ontology.object_versions v WHERE c.kind = 'c' AND b.from_object = c.object_id AND "
        "v.object_id = b.from_object AND v.l1_handle = b.evidence_handles::jsonb ->> 0 AND "
        "v.superseded_at IS NULL AND v.markings IS DISTINCT FROM b.markings")
    remarked = cur.rowcount
    cur.execute(
        f"DELETE FROM {TABLE} b USING bare_changed c WHERE b.from_object = c.object_id AND "
        "EXISTS (SELECT 1 FROM ontology.link_rows l WHERE l.from_object = b.from_object AND "
        "l.to_object = b.to_object AND l.link_type LIKE 'Email.mentions%%' AND "
        "l.superseded_at IS NULL)")
    return {"dropped_mail_gone": gone, "remarked": remarked, "dropped_served": cur.rowcount}


def _recheck_targets(conn: Any, cur: Any, stats: dict[str, int]) -> dict[str, int]:
    """SQL only, over ``bare_changed``'s changed customers, items and orders (module
    docstring of :func:`refresh`): the tokens their rows carry and the ones they now hold
    that a row carries, resolved again; a row whose (link type, target) is no longer among
    its token's holders goes. More than :data:`MAX_TARGETS` targets: left to the nightly
    build (``targets_deferred``)."""
    targets = conn.execute(
        "SELECT count(*) FROM bare_changed WHERE kind = 'c' AND object_id NOT LIKE 'Email:%%'"
    ).fetchone()[0]
    if not targets:
        return {"targets": 0}
    if targets > MAX_TARGETS:
        return {"targets": 0, "targets_deferred": targets}
    tokens = sorted({r[0] for r in conn.execute(
        f"SELECT DISTINCT {_token_sql()} FROM {TABLE} b JOIN bare_changed c ON "
        "c.object_id = b.to_object WHERE c.kind = 'c'").fetchall()} | {r[0] for r in conn.execute(
        "SELECT DISTINCT i.normalized FROM bare_changed c JOIN ontology.identifiers i ON "
        "i.object_id = c.object_id WHERE c.kind = 'c' AND i.identifier_kind = ANY(%s) AND "
        f"i.superseded_at IS NULL AND EXISTS (SELECT 1 FROM {TABLE} b WHERE "
        f"{_token_sql()} = i.normalized)",
        (sorted(KINDS),)).fetchall()})
    if not tokens:
        return {"targets": targets, "tokens": 0, "dropped_unheld": 0}
    holders = resolve(conn, tokens, stats)
    cur.execute("CREATE TEMP TABLE bare_valid (token text, link_type text, to_object text) "
                "ON COMMIT DROP")
    with cur.copy("COPY bare_valid (token, link_type, to_object) FROM STDIN") as copy:
        for tok, held in holders.items():
            for link_type, obj in held:
                copy.write_row((tok, link_type, obj))
    cur.execute(
        f"DELETE FROM {TABLE} b WHERE {_token_sql()} = ANY(%s) "
        "AND NOT EXISTS (SELECT 1 FROM bare_valid v WHERE v.token = "
        f"{_token_sql()} AND v.link_type = b.link_type AND "
        "v.to_object = b.to_object)", (tokens,))
    return {"targets": targets, "tokens": len(tokens), "dropped_unheld": cur.rowcount}


def _log_pending(conn: Any) -> int:
    """Inside a transaction: wait out a running merge, then, while a build is staging, log
    ``bare_changed``'s records for a refresh after its merge (the staged ranges may have
    read them before this change). The count logged; 0 when no build runs. A pending table
    no build holds (a build killed hard: OOM, a unit timeout) is dropped, never filled."""
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (MERGE_LOCK,))
    if conn.execute("SELECT to_regclass(%s)", (PENDING,)).fetchone()[0] is None:
        return 0
    if conn.execute("SELECT pg_try_advisory_lock(%s)", (BUILD_LOCK,)).fetchone()[0]:
        conn.execute("SELECT pg_advisory_unlock(%s)", (BUILD_LOCK,))
        conn.execute(f"DROP TABLE IF EXISTS {NEXT}, {PENDING}")
        return 0
    return conn.execute(f"INSERT INTO {PENDING} (object_id) SELECT object_id "
                        "FROM bare_changed").rowcount


def propose(handles: Sequence[str], state: str | None) -> list[dict[str, Any]]:
    """Read-only: the rows the build would hold for these mail handles."""
    from ontology.schema import load_schema
    check_schema(load_schema().link_types)
    store, conn = _open_store(state), _connect(read_only=True)
    try:
        mails: list[dict[str, Any]] = []
        for i in range(0, len(handles), 500):
            part = list(handles[i:i + 500])
            mails += read_mails(store, _exclusion(),
                                f"l1_handle IN ({', '.join('?' * len(part))})", part)
        return mail_rows(conn, mails, own_words(),
                         datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"))
    finally:
        conn.close()
        store.db.close()


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="operating.pg.bare_mentions")
    ap.add_argument("cmd", choices=("propose", "build", "clear", "count"))
    ap.add_argument("--handles", type=Path)
    ap.add_argument("--state")
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 1)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--if-migrated", action="store_true",
                    help="build: exit 0 doing nothing when migration 0035 is not applied "
                         "(the nightly unit before the window)")
    args = ap.parse_args(argv)
    if args.cmd == "propose":
        if args.handles is None:
            ap.error("propose needs --handles")
        handles = [h for h in args.handles.read_text().split() if h]
        for row in propose(handles, args.state):
            print(json.dumps(row, sort_keys=True))
        return 0
    if args.cmd == "build":
        if args.if_migrated:
            conn = _connect(read_only=True)
            try:
                migrated = table_exists(conn)
            finally:
                conn.close()
            if not migrated:
                print(json.dumps({"skipped": "not_migrated"}))
                return 0
        t0 = time.monotonic()
        report = build(args.workers, args.state, args.limit)
        print(json.dumps({**report, "seconds": round(time.monotonic() - t0, 1)},
                         sort_keys=True))
        return 0
    conn = _connect(read_only=args.cmd == "count")
    try:
        if args.cmd == "clear":
            conn.execute(f"TRUNCATE {TABLE}")
        print(json.dumps({"rows": conn.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0]}))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
