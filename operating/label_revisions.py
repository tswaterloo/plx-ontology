"""Label and artwork revision control: every revision of every label or artwork file the store
holds, per item and customer, who approved it and when, and the open sales and manufacturing
orders that still point at a superseded one. Run by ``operating.proactive_engine`` beside
``label_changes`` (``label_revisions``), and the source of that rule's "a new label file
came" and "the revision was approved" (``item_revisions``).

A revision is a mail attachment or a SharePoint file whose name (``parse``) names a label or
artwork (``label_changes.ART``; no private-label, shipping-label or proof-of-payment name, no
office document, only artwork and label file types, ``EXTENSIONS``) and carries an explicit
revision token: ``Rev 3``, ``rev.B``, ``v02``, ``Ver4``, ``Version 2`` (a date is no revision).
The name without its token, copy markers and file type is its family; the token is its rank
(numbers and letters are two schemes, never compared). One revision is every copy of one
family at one rank (the same file mailed to several mailboxes, the PDF and the AI file).

A family belongs to (``_scope``), in order: the one item its name's product code names
(``identifiers`` item codes, all of one settled ``Product.same_record_as`` cluster) or the
file's or its message's settled ``mentions_product`` link; else the customer of the file's
message (``Email.sent_by_customer`` / ``mentions_customer``) or file
(``SharePointFile.mentions_customer``), settled; else the outside party of the mail (the
first address outside the tenant's domains: its domain, a public mailbox its address), the
conversation of mail among ourselves only, or the SharePoint folder. Families of two scopes
are never merged.

Approval (``approves``): a later message of the carrying conversation, sent by someone other
than who sent the revision, that approves ("approved", "OK to print"; negated, asked, hedged,
reported or conditional, no: ``HEDGE``), before another revision of the family came in that conversation.
It records who (the sender's address), when and the message.
A SharePoint copy has no thread: its approval is unknown, never assumed.

Kinds:

* ``label_revision`` (low; never the briefing): a revision that still asks for an
  approval: the highest rank of its family name in any scope, with no approval recorded on
  any copy of that rank in any scope, raised once per family name and rank (an item scope
  first, then a customer, then the earliest seen); about its item, else its customer, else
  its first copy; its copies cited (any one readable shows it).
* ``label_revision_history`` (low; never an alert): every other revision (superseded,
  approved, or the same revision under another scope), its approval cited when it has one.
  The two kinds are the record page's history (``history``). Which revision is current is the
  page's: the highest rank the reader can see (``history``), so a revision kept from a reader
  never shows as superseding one they can see.
* ``label_revision_superseded`` (medium; OFF in the alert registry until measured: on the
  gen 12 store, 2026-11-09, no copy of any of 188 revisions carried a settled order mention,
  so there was nothing to check, n=0): an open
  sales order or manufacturing (assembly) order that a copy of a revision names (the copy's
  or its message's settled ``mentions_sales_order`` / ``mentions_assembly_order`` link) while
  a higher revision of the family exists and no copy of that higher revision names the order.
  A sales order's openness is the engine's one active-population predicate (closed and
  historic orders are dropped after the rule); an assembly order is open only by a status word
  of ``AO_OPEN`` and no ``deleted_at`` (an unknown or missing status is not judged). It
  requires a grant on every audience of both revisions' copies.

Measured read-only on the gen 12 store (2026-11-09): 391 revision files, 151 families, 188
revisions, 27 approvals, hand-checked 26 of 27 right (the reader was tuned on that sample;
no held-out sample).

People first, fail closed: with the guard unavailable or no tenant domain known the rule is
``blocked``; a copy in a mailbox, chat or drive the exclusion does not clear is dropped
unread, and a copy whose thread has a person of ours not cleared is not judged (and no
approval is read from it). Alerts store object ids, enum words, dates, a clipped file name and
the approver's address; never a body. Rule-based, no model call; fixture runs are
``isolated-fixture``.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
import json
import re
from typing import Any, Callable, Iterable, Mapping, Sequence

from .copy_gate import say_date
from . import label_changes as lc
from . import proactive_threads as pt
from . import test_records
from .proactive_business import _tick
from .proactive_chasers import _addr, _instant

RULE = "label_revisions"
REVISION = "label_revision"
HISTORY = "label_revision_history"
SUPERSEDED = "label_revision_superseded"
KINDS = (REVISION, HISTORY, SUPERSEDED)
ANCHORED = ("item", "customer")   # scopes ranked before a mail party, thread or folder
NAME_CHARS = 60
MAX_COPIES = 400        # copies of one family read (a longer one is a template library)
EXTENSIONS = frozenset({"pdf", "ai", "eps", "psd", "png", "jpg", "jpeg", "tif", "tiff", "svg",
                        "indd", "btw", "nlbl", "lbl", "cdr", "bmp", "gif", "webp"})
WORDS = lc.WORDS
FILE, ATTACHMENT = "SharePointFile", "Attachment"
ATTACHED_TO = "Attachment.attached_to"
ITEM_LINK = "mentions_product"
CUSTOMER_LINKS = ("Email.sent_by_customer", "Email.mentions_customer",
                  "SharePointFile.mentions_customer")
ORDER_LINKS = {"mentions_sales_order": "SalesOrder", "mentions_assembly_order": "AssemblyOrder"}
ITEM_CODES = ("item_code", "erp_item_code")
SETTLED = lc.SETTLED
# A manufacturing order still to be made: the production rule's open words and the Portal
# and FileMaker steps before production ends ("released" is ambiguous there: not judged).
AO_OPEN = frozenset({"open", "planned", "scheduled", "pending", "in progress", "in_production",
                     "in production", "in_progress", "started", "waiting", "on hold", "created",
                     "generated", "committed", "allocated"})

# The revision token: "rev 3", "rev.B", "revision 2", "v02", "_V2", "Ver4", "version 2". A
# letter revision needs a separator ("Revisions" is no revision S).
REV = re.compile(
    r"(?<![a-z0-9])(?:rev(?:ision)?|ver(?:sion)?)[\s._-]*(?P<n>\d{1,3})(?:\.\d)?(?![0-9])"
    r"|(?<![a-z0-9])rev(?:ision)?[\s._-]+(?P<a>[a-z])(?![a-z0-9])"
    r"|(?<![a-z0-9])v[\s._-]?(?P<v>\d{1,3})(?:\.\d)?(?![0-9])", re.I)
# Not artwork: a brand model, shipping and pallet labels, proofs of payment or delivery, and
# documents about labels (inquiries, quotes, reports, forms, evidence, specs, instructions,
# container drawings, approval forms, purchase orders; live 11-12: 4 of 15 asks).
NOT_ART = re.compile(
    lc.NOT_ART.pattern + r"|\b(?:inquiry|enquiry|rfq|quote|quotation|opportunity|report|"
    r"evidence|form|checklist|sheet|specification|verification|accountability|summary|"
    r"payment|delivery|shipment|retain|folders?|specs?|instructions?|drawings?|approvals?|"
    r"purchase\s+orders?)\b", re.I)
COPY_MARK = re.compile(r"\(\d{1,3}\)|\[\d{1,3}\]|\bcopy(?:\s+of)?\b|^\d{4}-\d{2}-\d{2}[ _-]*",
                       re.I)
DROP = frozenset({"final", "fnl", "outlined", "outline", "part", "pg", "page"})


def parse(name: Any) -> tuple[str, tuple[str, int], str] | None:
    """(family, rank, the token as written) of a label or artwork file name, or None (module
    doc): the rank is ("n", number) or ("a", letter position)."""
    if not isinstance(name, str) or "." not in name:
        return None
    stem, ext = name.rsplit(".", 1)
    if ext.strip().lower() not in EXTENSIONS:
        return None
    spaced = re.sub(r"[_]+", " ", stem)
    if not lc.ART.search(spaced) or NOT_ART.search(spaced):
        return None
    if test_records.is_test([stem]):
        return None   # a test or UAT file (live 11-12: synthetic UAT artwork references)
    found = list(REV.finditer(stem))
    if len(found) != 1:
        return None   # no token, or two (a family name holding a version): not judged
    m = found[0]
    if m.group("a"):
        rank = ("a", ord(m.group("a").lower()) - ord("a") + 1)
    else:
        rank = ("n", int(m.group("n") or m.group("v")))
    head, tail = stem[:m.start()], stem[m.end():]
    # a page or part number right after the token is the same revision ("Rev.1-06")
    tail = re.sub(r"^[\s._-]*(?:(?:part|pg|page)[\s._-]*)?\d{1,2}(?![0-9a-z])", " ", tail,
                  flags=re.I)
    rest = COPY_MARK.sub(" ", head + " " + tail)
    words = [w for w in re.findall(r"[a-z0-9]+", rest.lower()) if w not in DROP]
    if len(words) < 2 or not lc.ART.search(" ".join(words)):
        return None
    return " ".join(words), rank, m.group(0).strip(" ._-")


# --------------------------------------------------------------------------- the read

def _chunks(ids: Sequence[str]) -> Iterable[list[str]]:
    for start in range(0, len(ids), pt.CHUNK):
        yield list(ids[start:start + pt.CHUNK])


def _named(st: Any) -> dict[str, str] | None:
    """object -> name of every attachment and SharePoint file whose name holds a label word
    (the word index; None: no index here)."""
    out: dict[str, str] = {}
    # the two lookups side by side (each thread on its own store connection)
    with ThreadPoolExecutor(2) as pool:
        found = list(pool.map(lambda prefix: pt._word_ids(st, WORDS, ("name",), prefix=prefix),
                              (ATTACHMENT + ":%", FILE + ":%")))
    if any(f is None for f in found):
        return None
    for oid, row in pt._read(st, sorted(set().union(*found)), ("name",)).items():
        out[oid] = row["name"]["value"]
    return out


# object ids per name read when no word index serves the names (the whole scan)
SCAN_CHUNK = 2000
# At most this many chunks read side by side (each thread on its own store connection).
SCAN_THREADS = 8
# Postgres only: a name holding none of these is no label or artwork name (every word of
# ``label_changes.ART`` holds one), so it is dropped by the server, never shipped and
# tested here; ``ART`` still decides each name kept.
ART_SUPERSET = "label|art|dieline|proof|packaging|carton"


def _scan_named(st: Any) -> dict[str, str]:
    """The same as ``_named`` with no word index (Postgres until its value words are built):
    the attachments and files listed by the type index (the records removed now dropped by
    the run's removal read of those two types, ``_Store.removed``), their names read a chunk of ids at a
    time, side by side, and only the label-worded kept (on Postgres the server drops every
    name holding no word of ``ART_SUPERSET`` first). One statement over every name scanned
    the store's value cells (statement timeout) and held them all (over 4 GB,
    2026-11-11); one chunk after another took 336 s of the change rule's 465 s
    (2.25M names, 2026-11-12)."""
    from ontology import store as ostore
    removed = st.removed((ATTACHMENT, FILE)) if callable(getattr(st, "removed", None)) \
        else None
    if removed is None:
        ids = [row[0] for row in st.conn.execute(
            "SELECT o.object_id FROM objects o WHERE o.object_type IN (?, ?) AND NOT "
            + ostore.removed_at_sql("o.object_id"), (ATTACHMENT, FILE))]
    else:
        ids = [row[0] for row in st.conn.execute(
            "SELECT o.object_id FROM objects o WHERE o.object_type IN (?, ?)",
            (ATTACHMENT, FILE)) if row[0] not in removed]
    ids.sort()
    server = bool(getattr(st.conn, "postgres", False))
    sql = ("SELECT object_id, value FROM object_values WHERE superseded_at IS NULL AND "
           "property = 'name' AND object_id IN (SELECT value FROM json_each(?))"
           + (" AND value ~* ?" if server else ""))

    def one(chunk: list[str]) -> list[tuple[str, Any]]:
        args = (json.dumps(chunk), ART_SUPERSET) if server else (json.dumps(chunk),)
        return [(oid, value) for oid, value in st.conn.execute(sql, args)
                if isinstance(value, str) and lc.ART.search(value)]
    chunks = [ids[i:i + SCAN_CHUNK] for i in range(0, len(ids), SCAN_CHUNK)]
    workers = max(1, min(int(getattr(st, "threads", 1) or 1), SCAN_THREADS, len(chunks) or 1))
    out: dict[str, str] = {}
    pool = ThreadPoolExecutor(workers)
    try:
        for found in pool.map(one, chunks):
            _tick(st)   # the asking thread's window, as each chunk comes back
            for oid, value in found:
                out.setdefault(oid, value)
    finally:
        # a window spent: the chunks not started are never read
        pool.shutdown(wait=True, cancel_futures=True)
    return out


def _links_from(st: Any, oids: Sequence[str]) -> dict[str, list[tuple[str, str]]]:
    """object -> [(link type, to)] of its settled current links."""
    out: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for chunk in _chunks(sorted(set(oids))):
        for frm, link_type, to, tier in st.conn.execute(
                "SELECT from_object, link_type, to_object, tier FROM link_rows WHERE from_object "
                "IN (SELECT value FROM json_each(?)) AND superseded_at IS NULL",
                (json.dumps(chunk),)):
            if tier in SETTLED or link_type == ATTACHED_TO:
                out[frm].append((link_type, to))
    return out


def _audiences(st: Any, oids: Sequence[str]) -> dict[str, str]:
    from ontology import store as ostore
    out = {}
    for chunk in _chunks(sorted(set(oids))):
        for oid, key in st.conn.execute(
                "SELECT o.object_id, o.audience_key FROM objects o WHERE o.object_id IN (SELECT "
                "value FROM json_each(?)) AND NOT " + ostore.removed_at_sql("o.object_id"),
                (json.dumps(chunk),)):
            out[oid] = key
    return out


def _coded_items(st: Any, names: Iterable[str]) -> dict[str, str]:
    """product code (as normalised) -> the one item it names: every product holding the code
    in one settled ``Product.same_record_as`` cluster (its least id); an ambiguous code none."""
    from ontology import identifiers as idf
    codes = set()
    for name in names:
        for token in re.findall(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*", name.rsplit(".", 1)[0]):
            if len(token) >= 4 and re.search(r"[A-Za-z]", token) and re.search(r"\d", token) \
                    and not REV.fullmatch(token):
                value = idf.normalize("item_code", token)
                if value:
                    codes.add(value)
    holders: dict[str, set[str]] = defaultdict(set)
    for chunk in _chunks(sorted(codes)):
        try:
            rows = list(st.conn.execute(
                "SELECT normalized, object_id FROM identifiers WHERE identifier_kind IN (SELECT "
                "value FROM json_each(?)) AND normalized IN (SELECT value FROM json_each(?)) AND "
                "superseded_at IS NULL AND object_id LIKE 'Product:%'",
                (json.dumps(list(ITEM_CODES)), json.dumps(chunk))))
        except Exception as exc:  # noqa: BLE001 - a store with no identifier table: no codes
            if "interrupted" in str(exc):
                raise
            return {}
        for code, oid in rows:
            holders[code].add(oid)
    every = sorted({o for objs in holders.values() for o in objs})
    parent = {o: o for o in every}

    def top(o: str) -> str:
        while parent[o] != o:
            parent[o] = parent[parent[o]]
            o = parent[o]
        return o
    for frm, links in _links_from(st, every).items():
        for link_type, to in links:
            if link_type == "Product.same_record_as" and to in parent:
                a, b = sorted((top(frm), top(to)))
                parent[b] = a
    out = {}
    for code, objs in holders.items():
        tops = {top(o) for o in objs}
        if len(tops) == 1:
            out[code] = min(o for o in objs if top(o) in tops)
    return out


def population(st: Any, as_of: date, people: Any) -> dict[str, Any]:
    """The revisions, families and order pointers of the store, read once per run."""
    key = f"label_revisions:{as_of.isoformat()}"
    if callable(getattr(st, "memo", None)):
        return st.memo(key, lambda: _population(st, as_of, people))
    return _population(st, as_of, people)


def _population(st: Any, as_of: date, people: Any) -> dict[str, Any]:
    from ontology import identifiers as idf
    domains = pt._domains(people)
    counts: Counter = Counter()
    named = _named(st)
    indexed = named is not None
    if named is None:
        named = _scan_named(st)
    parsed = {o: p for o, n in named.items() if (p := parse(n)) is not None}
    counts["named"], counts["revision_files"] = len(named), len(parsed)
    audience = _audiences(st, sorted(parsed))
    cleared = {k: bool(people.unit_clear(k)) for k in set(audience.values())}
    kept = {o: p for o, p in parsed.items() if o in audience and cleared[audience[o]]}
    dropped = len(parsed) - len(kept)
    links = _links_from(st, sorted(kept))
    carrier = {o: next((to for t, to in links.get(o, ()) if t == ATTACHED_TO), None)
               for o in kept if o.startswith(ATTACHMENT + ":")}
    # the carrying messages and every message of their conversations (label_changes' read)
    emails = sorted({e for e in carrier.values() if e})
    pop = lc.thread_population(st, pt._read(st, emails, lc.MAIL_PROPS), set(emails), indexed,
                               people, as_of)
    dropped += pop["dropped_unread"]
    links.update(_links_from(st, emails))
    file_rows = pt._read(st, sorted(o for o in kept if o.startswith(FILE + ":")),
                         ("name", "last_modified", "path"))
    att_rows = pt._read(st, sorted(carrier), ("name",))
    threads: dict[str, pt._Thread] = {}
    codes = _coded_items(st, (named[o] for o in kept))
    copies = []
    for oid, (family, rank, token) in sorted(kept.items()):
        _tick(st)
        own_links = links.get(oid, ())
        if oid in carrier:
            email = carrier[oid]
            row = pop["rows"].get(email or "")
            if row is None:
                counts["message_unread"] += 1
                continue
            conversation = (row.get("conversation_id") or {}).get("value")
            thread = None
            if conversation:
                if conversation not in threads:
                    threads[conversation] = pt._Thread(pop, conversation, people, domains)
                thread = threads[conversation]
                if not thread.ok:
                    counts[thread.why] += 1
                    continue
            at = _instant((row.get("sent_at") or {}).get("value"))
            sender = _addr((row.get("sender") or {}).get("value"))
            if at is None or (sender and pt._internal(sender, domains)
                              and not people.identity_clear(sender)):
                counts["sender_not_cleared" if at else "undated"] += 1
                continue
            carried = list(own_links) + list(links.get(email, ()))
            party = next((pt._party(a, domains) for a in [sender] + list(pt._to(row) or ())
                          if a and not pt._internal(a, domains)), None)
            # mail among ourselves only: its own conversation, never a name across threads
            fallback = ("party:" + party) if party else (
                "thread:" + conversation) if conversation else None
            cite_rows = [att_rows[oid]["name"]] if "name" in att_rows.get(oid, {}) else []
            cite_rows += [row[p] for p in ("sent_at", "subject") if p in row]
            copies.append({"oid": oid, "channel": "mail", "at": at, "sender": sender,
                           "email": email, "thread": thread, "links": carried,
                           "audience": {audience[oid], pop["audience"].get(email, "")} - {""},
                           "scope_fallback": fallback,
                           "family": family, "rank": rank, "token": token,
                           "name": named[oid], "cite_rows": cite_rows})
        else:
            row = file_rows.get(oid, {})
            at = _instant((row.get("last_modified") or {}).get("value"))
            if at is None:
                counts["undated"] += 1
                continue
            path = (row.get("path") or {}).get("value") or ""
            folder = path.rsplit("/", 1)[0] if "/" in path else ""
            drive = oid.split(":", 2)[-1]
            copies.append({"oid": oid, "channel": "sharepoint", "at": at, "sender": "",
                           "email": None, "thread": None, "links": list(own_links),
                           "audience": {audience[oid]},
                           "scope_fallback": "folder:" + _drive_of(drive) + folder.lower(),
                           "family": family, "rank": rank, "token": token,
                           "name": named[oid],
                           "cite_rows": [row[p] for p in ("name", "last_modified") if p in row]})
    # each copy's scope (module doc), then the families
    families: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for copy in copies:
        items = sorted({to for t, to in copy["links"] if t.endswith("." + ITEM_LINK)})
        coded = sorted({codes[c] for c in (idf.normalize("item_code", tok) for tok in
                        re.findall(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*",
                                   copy["name"].rsplit(".", 1)[0])) if c in codes})
        customers = sorted({to for t, to in copy["links"] if t in CUSTOMER_LINKS})
        named_items = sorted(set(items) | set(coded))
        if len(named_items) == 1:
            scope = "item:" + named_items[0]
        elif named_items:
            counts["items_ambiguous"] += 1
            continue
        elif len(customers) == 1:
            scope = "customer:" + customers[0]
        elif customers or not copy["scope_fallback"]:
            counts["scope_ambiguous"] += 1
            continue
        else:
            scope = copy["scope_fallback"]
        copy["scope"] = scope
        families[(scope, copy["family"])].append(copy)
    revisions = []
    for (scope, family), members in sorted(families.items()):
        _tick(st)
        if len(members) > MAX_COPIES:
            counts["library_families"] += 1
            continue
        if len({c["rank"][0] for c in members}) > 1:
            counts["mixed_schemes"] += 1
            continue
        by_rank: dict[tuple[str, int], list[dict]] = defaultdict(list)
        for c in members:
            by_rank[c["rank"]].append(c)
        for rank, group in sorted(by_rank.items()):
            group.sort(key=lambda c: (c["at"], c["oid"]))
            revisions.append({"scope": scope, "family": family, "rank": rank,
                              "token": group[0]["token"], "copies": group,
                              "first_at": group[0]["at"],
                              "approval": _approval(group, members)})
    counts["families"] = len(families)
    counts["revisions"] = len(revisions)
    return {"revisions": revisions, "dropped_unread": dropped, "counts": counts,
            "indexed": indexed}


def _drive_of(native: str) -> str:
    try:
        key = json.loads(native)
    except ValueError:
        return ""
    return (str(key.get("drive_id") or "") + ":") if isinstance(key, dict) else ""


# An approval sentence that asks, hedges, reports or names something approved before
# ("please confirm the proof is approved", "looks good but maybe", "the approved colour chips",
# "sign off upon approval", "can we go ahead", "should these be approved", "has already been
# approved", "artworks were usually approved", "they confirmed that ... okay to proceed"): no
# approval of this revision.
HEDGE = re.compile(
    r"\b(?:please|could|would|should|can\s+you|let\s+me\s+know|if|whether|almost|but|"
    r"maybe|review|once|when|upon|after|pending|said|already|can\s+(?:\w+\s+)?go\s+ahead|"
    r"approval\s+form|usually|typically|generally|normally|confirmed\s+that)\b|"
    r"\bwere\s+(?:\w+\s+)?approved\b|"
    r"\bthe\s+approved\b|\bapproved\s+(?:colou?rs?|chips?|samples?|versions?|files?|"
    r"artworks?|proofs?|labels?)\b", re.I)


def approves(text: Any) -> bool:
    """Whether a message approves the revision it answers: a sentence of its own text that
    ``label_changes.approval`` reads as an approval and that neither asks nor hedges."""
    for sentence in pt._sentences(text if isinstance(text, str) else ""):
        if lc.APPROVAL.search(sentence) and not lc.NOT_APPROVAL.search(sentence) \
                and not HEDGE.search(sentence):
            return True
    return False


def _approval(copies: Sequence[Mapping[str, Any]], family: Sequence[Mapping[str, Any]]
              ) -> dict[str, Any] | None:
    """The first approval of a revision: a later message of a carrying conversation, by
    someone other than its sender, that approves (module doc), before another revision of
    the family came in that conversation (an approval after revision 3 is revision 3's)."""
    best = None
    for c in copies:
        thread = c["thread"]
        if thread is None:
            continue
        until = min((o["at"] for o in family if o["thread"] is thread
                     and o["rank"] != c["rank"] and o["at"] > c["at"]), default=None)
        for oid in thread.messages:
            at = thread.sent(oid)
            sender = thread.sender(oid)
            if until is not None and at >= until:
                break
            if at <= c["at"] or not sender or sender == c["sender"]:
                continue
            if approves(thread.text(oid)) and (best is None or at < best["at"]):
                best = {"at": at, "by": sender, "oid": oid,
                        "rows": [thread.rows[oid][p] for p in ("sent_at", "subject", "sender")
                                 if p in thread.rows[oid]]}
                break
    return best


# --------------------------------------------------------------------------- the rule

def _assembly_open(st: Any, orders: Sequence[str]) -> dict[str, tuple[bool | None, list]]:
    """assembly order -> (open: True, closed: False, unknown: None; its status rows)."""
    rows = pt._read(st, orders, ("status", "deleted_at"))
    out = {}
    for oid in orders:
        row = rows.get(oid, {})
        status = str((row.get("status") or {}).get("value") or "").strip().lower()
        cite = [row[p] for p in ("status",) if p in row]
        if (row.get("deleted_at") or {}).get("value"):
            out[oid] = (False, cite)
        elif status in AO_OPEN:
            out[oid] = (True, cite)
        elif status in {"realized", "cancelled", "canceled", "closed", "complete",
                        "completed", "done", "finished", "shipped", "void"}:
            out[oid] = (False, cite)
        else:
            out[oid] = (None, cite)
    return out


def _subject(scope: str, first: str) -> tuple[str, str]:
    head, _, rest = scope.partition(":")
    if head in ("item", "customer") and rest:
        return rest, rest.split(":", 1)[0]
    return first, first.split(":", 1)[0]


def label_revisions(st: Any, as_of: date, *, gap: Callable, cites: Callable, result: Callable,
                    skips: Callable, people: Any, **_: Any) -> dict[str, Any]:
    blocked = pt._blocked(people, result)
    if blocked is not None:
        return blocked
    pop = population(st, as_of, people)
    skipped = skips()
    if pop["dropped_unread"]:
        skipped["not_cleared_unread"] += pop["dropped_unread"]
    for why in ("participants_not_cleared", "sender_not_cleared", "message_unread"):
        if pop["counts"][why]:
            skipped[why] += pop["counts"][why]
    counts: Counter = Counter()
    gaps = []
    by_family: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for rev in pop["revisions"]:
        by_family[(rev["scope"], rev["family"])].append(rev)
    # what a revision still asks for is read across every scope holding its family name:
    # the same file mailed in two threads and kept in a folder is three scopes (live 11-12:
    # 8 of 15 were a superseded or approved revision, or one revision raised per scope)
    named: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for rev in pop["revisions"]:
        named[(rev["family"], rev["rank"][0])].append(rev)
    canonical: dict[tuple, int] = {}
    for rev in pop["revisions"]:
        same = named[(rev["family"], rev["rank"][0])]
        if rev["rank"] < max(r["rank"] for r in same) or any(
                r["approval"] for r in same if r["rank"] >= rev["rank"]):
            continue
        head = rev["scope"].split(":", 1)[0]
        order = (ANCHORED.index(head) if head in ANCHORED else len(ANCHORED),
                 rev["first_at"], rev["scope"])
        key = (rev["family"], rev["rank"])
        if key not in canonical or order < canonical[key][0]:
            canonical[key] = (order, id(rev))
    asks = {ident for _order, ident in canonical.values()}
    for rev in pop["revisions"]:
        _tick(st)
        first = rev["copies"][0]
        subject_id, subject_type = _subject(rev["scope"], first["oid"])
        same = named[(rev["family"], rev["rank"][0])]
        newest = max(same, key=lambda r: (r["rank"], r["first_at"]))
        current = rev["rank"] == newest["rank"]
        approved_later = next((r for r in sorted(same, key=lambda r: r["rank"])
                               if r["rank"] >= rev["rank"] and r["approval"]), None)
        kind = REVISION if id(rev) in asks else HISTORY
        groups = [cites([r for c in rev["copies"] for r in c["cite_rows"]])]
        approval = rev["approval"]
        if approval:
            groups.append(cites(approval["rows"]))
        if kind == REVISION:
            missing = "Record who approved this revision before it is printed"
        elif not current:
            missing = (f"Nothing: revision {newest['token']} of this file came after it; "
                       "kept as label revision history")
        elif approved_later:
            missing = "Nothing: kept as this item's label revision history"
        else:
            missing = ("Nothing: the same revision is kept under another record or "
                       "conversation; kept as label revision history")
        made = gap(
            kind, RULE, subject_id, subject_type,
            f"labelrev|{rev['scope']}|{rev['family']}|{rev['rank'][0]}{rev['rank'][1]}",
            summary=(f"{pt._clip(first['name'], NAME_CHARS)}: revision {rev['token']}, first "
                     f"seen {say_date(rev['first_at'].date())}"
                     + (f", approved {say_date(approval['at'].date())}" if approval else
                        ", no approval recorded")
                     + ("" if current else f", superseded by revision {newest['token']}")),
            missing=missing,
            why=("A label or artwork file whose name carries a revision; its mail thread's "
                 "later messages were read for an approval (the copies cited)."),
            priority="low", about=(subject_type, "label_revision"), groups=groups,
            event={"family": rev["family"], "scheme": rev["rank"][0], "rank": rev["rank"][1],
                   "revision": rev["token"], "name": pt._clip(first["name"], NAME_CHARS),
                   "first_seen": rev["first_at"].date().isoformat(),
                   "scope": rev["scope"].split(":", 1)[0],
                   "channels": sorted({c["channel"] for c in rev["copies"]}),
                   "copies": [{"id": c["oid"], "channel": c["channel"],
                               "on": c["at"].date().isoformat()}
                              for c in rev["copies"][:5]],
                   "approved_on": approval["at"].date().isoformat() if approval else None,
                   "approved_by": approval["by"] if approval else None,
                   "approval_message": approval["oid"] if approval else None,
                   "current_in_family": current})
        if made is None:
            skipped["revision_uncited"] += 1
            continue
        made["requires"] = _audiences_of(rev["copies"])
        gaps.append(made)
        counts[kind] += 1
        counts["approved" if approval else "unapproved"] += 1
    # open orders a superseded revision's copy names
    pointers: dict[tuple, dict[str, list]] = {}
    aos: set[str] = set()
    for key, revs in by_family.items():
        for rev in revs:
            named_orders: dict[str, list] = defaultdict(list)
            for c in rev["copies"]:
                for link_type, to in c["links"]:
                    kind = link_type.rsplit(".", 1)[-1]
                    if kind in ORDER_LINKS and to.split(":", 1)[0] == ORDER_LINKS[kind]:
                        named_orders[to].append(c)
                        if ORDER_LINKS[kind] == "AssemblyOrder":
                            aos.add(to)
            pointers[(key, rev["rank"])] = named_orders
            counts["order_pointers"] += len(named_orders)
        by_family[key] = sorted(revs, key=lambda r: r["rank"])
    ao_state = _assembly_open(st, sorted(aos))
    for key, revs in sorted(by_family.items()):
        newest = revs[-1]
        for rev in revs[:-1]:
            for order, copies in sorted(pointers[(key, rev["rank"])].items()):
                _tick(st)
                if any(order in pointers[(key, r["rank"])] for r in revs
                       if r["rank"] > rev["rank"]):
                    counts["order_has_newer"] += 1
                    continue
                order_type = order.split(":", 1)[0]
                status_rows: list = []
                if order_type == "AssemblyOrder":
                    state, status_rows = ao_state.get(order, (None, []))
                    if state is None:
                        skipped["order_status_unknown"] += 1
                        continue
                    if not state:
                        counts["order_closed"] += 1
                        continue
                newer = rev_after(revs, rev)
                groups = [cites([r for c in copies for r in c["cite_rows"]]),
                          cites([r for c in newer["copies"] for r in c["cite_rows"]])]
                if status_rows:
                    groups.append(cites(status_rows))
                old_name = pt._clip(copies[0]["name"], NAME_CHARS)
                made = gap(
                    SUPERSEDED, RULE, order, order_type,
                    f"labelsup|{key[0]}|{key[1]}|{order}",
                    summary=(f"An open {'manufacturing' if order_type == 'AssemblyOrder' else 'sales'}"
                             f" order points at {old_name} (revision {rev['token']}); revision "
                             f"{newest['token']} came {say_date(newer['first_at'].date())}"),
                    missing="Confirm the order runs on the current revision, or update it",
                    why=("A label or artwork file naming this order carries a revision that a "
                         "later revision of the same file replaced (both cited)."),
                    priority="medium", about=(order_type, "label_revision"), groups=groups,
                    event={"family": key[1], "scope": key[0].split(":", 1)[0],
                           "subject": _subject(key[0], copies[0]["oid"])[0],
                           "revision": rev["token"], "current": newest["token"],
                           "superseded_on": newer["first_at"].date().isoformat(),
                           "current_approved_on": (newest["approval"]["at"].date().isoformat()
                                                   if newest["approval"] else None),
                           "name": old_name, "order": order,
                           "days": pt._business_days(newer["first_at"].date(), as_of)})
                if made is None:
                    skipped["order_uncited"] += 1
                    continue
                # the absence (no newer copy names the order) rests on every newer copy
                made["requires"] = _audiences_of(copies + [
                    c for r in revs if r["rank"] > rev["rank"] for c in r["copies"]])
                gaps.append(made)
                counts[SUPERSEDED] += 1
    out = result(gaps, not_evaluated=skipped)
    out["counts"] = {**{k: pop["counts"][k] for k in (
        "named", "revision_files", "families", "revisions", "items_ambiguous",
        "scope_ambiguous", "mixed_schemes", "library_families")},
        **{k: counts[k] for k in (REVISION, HISTORY, SUPERSEDED, "approved", "unapproved",
                                  "order_pointers", "order_has_newer", "order_closed")}}
    return out


def _audiences_of(copies: Iterable[Mapping[str, Any]]) -> list[str]:
    """Every audience a verdict on these copies rests on: each copy's own, its message's and
    its whole thread's (an approval, or none, is read from the thread)."""
    out: set[str] = set()
    for c in copies:
        out.update(c["audience"])
        if c["thread"] is not None:
            out.update(c["thread"].audiences)
    return sorted(out)


def rev_after(revs: Sequence[Mapping[str, Any]], rev: Mapping[str, Any]) -> Mapping[str, Any]:
    """The revision right above ``rev`` in its family (``revs`` sorted by rank)."""
    return next(r for r in revs if r["rank"] > rev["rank"])


# --------------------------------------------------------------------------- label_changes

def item_revisions(st: Any, as_of: date, people: Any
                   ) -> dict[str, list[dict[str, Any]]]:
    """item -> its revisions (first seen, first copy, approval, audiences, cite rows): the
    source of ``label_changes``' new label file and revision approval."""
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for rev in population(st, as_of, people)["revisions"]:
        head, _, item = rev["scope"].partition(":")
        if head != "item":
            continue
        out[item].append({"at": rev["first_at"], "oid": rev["copies"][0]["oid"],
                          "approval": rev["approval"],
                          "audience": sorted({a for c in rev["copies"] for a in c["audience"]}),
                          "rows": [r for c in rev["copies"] for r in c["cite_rows"]]})
    return out


# --------------------------------------------------------------------------- the page section

def history(gaps: Iterable[Mapping[str, Any]], object_id: str) -> dict[str, Any]:
    """The record's label and artwork revisions from the reader's own trimmed alert list,
    one family per file, newest revision first; current: the highest rank this reader can
    see. On an order: the superseded revisions it points at."""
    families: dict[tuple[str, str], list[dict]] = defaultdict(list)
    on_orders = []
    for item in gaps:
        event = item.get("event") if isinstance(item.get("event"), Mapping) else {}
        if item.get("kind") == SUPERSEDED and item.get("object_id") == object_id:
            on_orders.append({"id": item.get("id"), "name": event.get("name"),
                              "revision": event.get("revision"),
                              "current": event.get("current"),
                              "superseded_on": event.get("superseded_on"),
                              "summary": item.get("summary"),
                              "citations": list(item.get("citations") or ())})
            continue
        if item.get("kind") not in (REVISION, HISTORY) or item.get("object_id") != object_id:
            continue
        families[(str(event.get("family")), str(event.get("scheme")))].append({
            "id": item.get("id"), "revision": event.get("revision"),
            "rank": event.get("rank") if isinstance(event.get("rank"), int) else 0,
            "name": event.get("name"), "first_seen": event.get("first_seen"),
            "channels": list(event.get("channels") or ()),
            "approved_on": event.get("approved_on"), "approved_by": event.get("approved_by"),
            "citations": list(item.get("citations") or ())})
    out = []
    for (family, _scheme), revs in sorted(families.items()):
        revs.sort(key=lambda r: r["rank"], reverse=True)
        for i, r in enumerate(revs):
            r["current"] = i == 0
            r["superseded_on"] = revs[i - 1]["first_seen"] if i else None
        out.append({"family": family, "current": revs[0]["revision"],
                    "current_approved": bool(revs[0]["approved_on"]), "revisions": revs})
    out.sort(key=lambda f: max((r["first_seen"] or "") for r in f["revisions"]), reverse=True)
    return {"object_id": object_id, "families": out,
            "revisions": sum(len(f["revisions"]) for f in out),
            "unapproved_current": sum(not f["current_approved"] for f in out),
            "superseded_on_orders": on_orders}
