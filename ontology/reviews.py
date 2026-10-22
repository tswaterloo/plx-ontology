"""Human review decisions over the ontology: an append-only store and their projection.

A person's correction survives a
restart and changes the next investigation. Decisions, each by one authenticated
employee, timestamped, citing the exact record versions (L1 handle and content digest)
the person reviewed:

* ``accept_link`` / ``reject_link``: a declared link type between two records is
  confirmed or refused (a mention lead becomes ``reviewed``, or ``contradicted``);
* ``alias``: an identifier (one kind of ``ontology/schema/identifiers.json``, normalized
  by the one grammar) names the subject record, read in a cited field of the evidence:
  Vince's confirmed supplier number becomes a persistent alias of the PO;
* ``reject_alias``: the same identifier does not name the subject record (a person
  turned down an alias lead, ``alias_proposals``). It shares the alias's key, so the
  later of the two is the one in force: while a rejection holds the alias joins nothing
  (``alias_spans`` gives only ``alias`` stretches) and the build proposes it no more
  (``BuildAliases.rejected``; decision 2026-10-19-reference-alias-leads);
* ``document_kind``: a document record is of a Portal document type (history only: what a
  document is is recorded by ``plx.document_fields`` alone, see below);
* ``allocate``: of the subject's (the supply's) quantity property, a quantity in a unit
  goes to the target (the demand), over a link type the schema declares many-to-many
  between them (ER-11: one FM AO serving several customer orders). Allocations are
  never inferred: ``plx.aggregate`` splits a measure over such a link only by these.
  On one pair (a link type between the same two records, either order) the latest of
  its ``allocate`` and ``reject_link`` decisions is the only one in force (decision
  2026-10-08-allocation-latest-decision, ``_overruled``): a later ``reject_link``
  supersedes an allocation (a person who says the supply does not serve the demand has
  taken its share back) and a later ``allocate`` supersedes a rejection (``state_at``:
  ``superseded``, ``later_review``; the projection ends its link there). Only the
  overruling decision's retraction gives the earlier one back: re-accepting a rejected
  link does not revive the allocation before the rejection (a new ``allocate`` does).
  Overruling a rejection changes identity linkage: an ``allocate`` on a pair holding a
  ``reject_link`` no retraction names, and the retraction of an ``allocate`` that
  overrules one (it puts the rejection back in force), are appended only through the
  caller's identity gate (``record``'s ``identity_gate``: ``plx.review``'s
  ``review:identity``), else ``identity_review_required`` and nothing is written.
  While a rejection holds, the pair is its ``contradicted`` link alone: the projection
  ends the links of the other ``post_project`` steps (a mention ``lead``) on it.
  An ``alias`` derived from a link review (its author's ``derived_from``: written by the
  legacy ledger import, retired with ``operating/po_mail_links.py``) holds only while
  that link's key has an acceptance in force: a later ``reject_link`` of the link, or a
  retraction leaving none, withdraws the alias over that stretch (``alias_spans``;
  ``state_at``: ``withdrawn``, ``link_not_accepted``), its history kept; a link
  acceptance in force again holds it again.
* ``confirm_value`` / ``correct_value``: a person read one value of the subject (its
  ``property``) in the original and confirms the machine-read value (citing that value's
  own citation, the value recorded as read) or corrects it (the value, its unit and
  currency as the person read them, citing the page or span they read it at). Both
  share one key per subject and property, so the later one supersedes. Never projected
  into the store: ``plx.object`` shows a decision in force beside the source value
  (``operating.ontology_tools_review.show_value_reviews``), which it never overwrites.

A correction is a new record, never an edit: a later decision on the same key (the
same link, the same alias of the same record, the same allocation) supersedes the
earlier one, and ``retract`` withdraws a named review (the decision before it, if any,
applies again). The table refuses UPDATE and DELETE (triggers) and this module has no
path for either. The file lives in the Workbench state directory; nothing here writes
to any business system.

Withdrawal is automatic and never stored: a review applies only while every record
version it cites is still the current version of its record (same digest) and the
source still holds that record. A new capture with other content withdraws it
(``state_at``: ``withdrawn``, ``evidence_changed``), except that a link decision pins
only a cited purchase order's identity (``same_po_identity``: its row, numbers and
supplier; ER-09, the rule the projection's reviewed links and ``case_mail`` apply too): a
status-only change or an in-place Portal renumber to the BC number keeps it, any other
number or supplier change withdraws it. The source's removal of a cited record
withdraws it too (``source_removed``): any stretch of its ``object_removals``, a record
missing from its source (a deleted mail) or one the source soft-deleted (a Portal
``deleted_at``). The projection ends its rows at that capture or removal and, should
the record return, starts them again from the return, so ``as_known_at`` reads it
held, withdrawn inside the removal and held again.

Projection (``link_step``): a ``build_store`` ``post_project`` step writes every link
decision into the generation being built as a ``reviewed`` (accept) or ``contradicted``
(reject) link with binding ``reviewed``, the reviewer, time and review id, the cited
handles as evidence and the union of their markings, valid while the decision held and
its evidence was current (of a link decision's cited purchase order: its identity). An
allocation is a ``reviewed`` link of its declared type
(stored in the declared from -> to order) whose ``person_allocated`` reason feature
carries the supply, property, quantity and unit: a reviewed allocation. Aliases are not
projected into the identifier index (an identifier row must cite a version of its own
record, and the alias is read in another record): ``ontology.resolve.ReviewedAliases``
answers them for ``plx.find`` from this store at query time, so an alias applies from
the next call. A build resolves document mentions through them too (``BuildAliases``:
``ontology.mentions`` and ``ontology.document_fields`` read them beside the identifier
index), so a later document naming a confirmed supplier number links to the record it
names. Both build an alias's index row with the one ``alias_rows`` (lineage ``workbench``,
the cited field, the union of the cited versions' markings, valid from when the decision
and every cited version held); each keeps only its own gating (the build: the versions
this generation holds; ``plx.find``: the versions current at the reader's time that the
reader may read).

Document kinds are not held here: ``plx.document_fields`` is the one path that records what
a document is (``operating.kind_confirmation``, one store, projected onto the
``SharePointFile``); ``plx.review`` and ``record`` refuse ``document_kind``. Decisions
recorded here before that stay in this append-only history;
``KindConfirmations.import_reviews`` copies the ones in force, under their own review ids,
into that store, where ``plx.review`` lists and retracts them.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import secrets
import sqlite3
from typing import Any, Callable, Iterable, Mapping, Sequence

from ontology.store import (canonical, held_stretches, instant, markings_text,
                            removal_stretches)

FORMAT = "plx-reviews/v4"
DECISIONS = ("accept_link", "reject_link", "alias", "document_kind", "allocate",
             "confirm_value", "correct_value", "retract", "reject_alias")
ALIAS_DECISIONS = frozenset({"alias", "reject_alias"})
LINK_DECISIONS = frozenset({"accept_link", "reject_link"})
VALUE_DECISIONS = frozenset({"confirm_value", "correct_value"})
STATES = ("effective", "superseded", "retracted", "withdrawn", "unverified")
STORE_FILE = "reviews.sqlite"
ALLOCATED = "person_allocated"   # the reason feature of a reviewed allocation link
ALIAS_PROPERTY = "reviewed_alias"   # the index property of a reviewed alias row
ALIAS_LINEAGE = "workbench"   # a person's assertion, citing the version it is read in

_TABLE = f"""CREATE TABLE IF NOT EXISTS reviews (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        review_id TEXT NOT NULL UNIQUE,
        tenant TEXT NOT NULL,
        review_key TEXT NOT NULL,
        decision TEXT NOT NULL CHECK (decision IN ({", ".join(repr(d) for d in DECISIONS)})),
        subject TEXT NOT NULL,
        target TEXT,
        link_type TEXT,
        alias_kind TEXT,
        alias_normalized TEXT,
        alias_raw TEXT,
        document_kind TEXT,
        allocation_property TEXT,
        quantity TEXT,
        unit TEXT,
        value_property TEXT,
        value_text TEXT,
        currency TEXT,
        retracts TEXT REFERENCES reviews(review_id),
        evidence TEXT NOT NULL CHECK (json_valid(evidence) AND json_type(evidence) = 'array'),
        author TEXT NOT NULL CHECK (json_valid(author)),
        recorded_at TEXT NOT NULL,
        CHECK (decision = 'retract' OR json_array_length(evidence) > 0),
        CHECK ((decision = 'retract') = (retracts IS NOT NULL)),
        CHECK ((decision = 'allocate') = (quantity IS NOT NULL)),
        CHECK (decision <> 'allocate' OR (target IS NOT NULL AND link_type IS NOT NULL
               AND allocation_property IS NOT NULL AND unit IS NOT NULL)),
        CHECK ((decision IN ('confirm_value', 'correct_value'))
               = (value_property IS NOT NULL AND value_text IS NOT NULL)))"""
_DDL = (
    "CREATE TABLE IF NOT EXISTS review_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    _TABLE,
    "CREATE INDEX IF NOT EXISTS reviews_by_key ON reviews(tenant, review_key, seq)",
    "CREATE INDEX IF NOT EXISTS reviews_by_subject ON reviews(tenant, subject)",
    "CREATE INDEX IF NOT EXISTS reviews_by_target ON reviews(tenant, target)",
    "CREATE INDEX IF NOT EXISTS reviews_by_alias ON reviews(tenant, alias_kind, alias_normalized)",
    # Append-only: a correction is a new record.
    "CREATE TRIGGER IF NOT EXISTS reviews_no_update BEFORE UPDATE ON reviews "
    "BEGIN SELECT RAISE(ABORT, 'reviews_append_only'); END",
    "CREATE TRIGGER IF NOT EXISTS reviews_no_delete BEFORE DELETE ON reviews "
    "BEGIN SELECT RAISE(ABORT, 'reviews_append_only'); END",
)


class ReviewError(ValueError):
    """A review refused; ``code`` is stable."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def store_path(state_dir: str | Path) -> Path:
    """The review store of a Workbench state directory."""
    return Path(state_dir) / STORE_FILE


def review_key(decision: str, subject: str, *, target: str | None = None,
               link_type: str | None = None, alias_kind: str | None = None,
               alias_normalized: str | None = None,
               allocation_property: str | None = None,
               value_property: str | None = None) -> str:
    """What a decision is about: a later decision with the same key supersedes it (an
    allocation: its supply, demand, link type and the property it splits; a value
    confirmation or correction: its subject and property)."""
    if decision in LINK_DECISIONS:
        parts: list[Any] = ["link", subject, target, link_type]
    elif decision in ALIAS_DECISIONS:
        parts = ["alias", subject, alias_kind, alias_normalized]
    elif decision == "document_kind":
        parts = ["document_kind", subject]
    elif decision == "allocate":
        parts = ["allocate", subject, target, link_type, allocation_property]
    elif decision in VALUE_DECISIONS:
        parts = ["value", subject, value_property]
    else:
        raise ReviewError("decision_invalid")
    return hashlib.sha256(canonical(parts).encode("utf-8")).hexdigest()


def _record(row: sqlite3.Row) -> dict[str, Any]:
    out = {"seq": row["seq"], "review_id": row["review_id"], "tenant": row["tenant"],
           "review_key": row["review_key"], "decision": row["decision"],
           "subject": row["subject"], "evidence": json.loads(row["evidence"]),
           "author": json.loads(row["author"]), "recorded_at": row["recorded_at"]}
    for column in ("target", "link_type", "document_kind", "retracts"):
        if row[column] is not None:
            out[column] = row[column]
    if row["alias_kind"] is not None:
        out["alias"] = {"identifier_kind": row["alias_kind"],
                        "normalized": row["alias_normalized"], "raw": row["alias_raw"]}
    if row["quantity"] is not None:
        out["allocation"] = {"property": row["allocation_property"],
                             "quantity": row["quantity"], "unit": row["unit"]}
    if row["value_property"] is not None:
        out["value"] = {"property": row["value_property"], "value": row["value_text"]}
        for column in ("unit", "currency"):
            if row[column] is not None:
                out["value"][column] = row[column]
    return out


def _allocation(allocation: Mapping[str, Any] | None) -> dict[str, str]:
    """An allocation's property, quantity (a non-negative decimal as plain text) and
    unit, else ``allocation_invalid``."""
    try:
        prop, unit = allocation["property"], allocation["unit"]
        quantity = Decimal(str(allocation["quantity"]))
    except (TypeError, KeyError, InvalidOperation):
        raise ReviewError("allocation_invalid") from None
    if not (isinstance(prop, str) and prop and isinstance(unit, str) and unit.strip()
            and quantity.is_finite() and quantity >= 0):
        raise ReviewError("allocation_invalid")
    return {"property": prop, "unit": unit,
            "quantity": format(quantity.normalize() if quantity else Decimal(0), "f")}


def _value(value: Mapping[str, Any] | None) -> dict[str, str]:
    """A value review's property and value text, with its unit and currency when given
    (the tool has typed them against the schema), else ``value_invalid``."""
    if not isinstance(value, Mapping):
        raise ReviewError("value_invalid")
    out = {key: value[key] for key in ("property", "value", "unit", "currency")
           if value.get(key) is not None}
    if not (all(isinstance(v, str) and v for v in out.values())
            and "property" in out and "value" in out):
        raise ReviewError("value_invalid")
    return out


def _migrate(db: sqlite3.Connection) -> None:
    """Copy an older ``reviews`` table (its CHECK names only the decisions of its
    format: v1 before ``allocate``, v2 before the value reviews, v3 before
    ``reject_alias``) row for row, sequence and ids kept, into the current one; its
    triggers and indexes go with it and are recreated by the DDL. Nothing is edited or
    dropped from the history."""
    have = [row[1] for row in db.execute("PRAGMA table_info(reviews)")]
    sql = db.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND "
                     "name = 'reviews'").fetchone()
    if not have or ("value_property" in have and "'reject_alias'" in sql[0]):
        return
    columns = ", ".join(have)   # every older column is a column of the current table
    db.execute("ALTER TABLE reviews RENAME TO reviews_old")
    db.execute(_TABLE)
    db.execute(f"INSERT INTO reviews ({columns}) SELECT {columns} FROM reviews_old "
               "ORDER BY seq")
    db.execute("DROP TABLE reviews_old")


class ReviewStore:
    """Review decisions in the Workbench's own SQLite file: ``record`` appends, the rest
    reads. ``clock`` returns the UTC now a record is stamped with (never earlier than the
    tenant's last record, so store order is time order)."""

    def __init__(self, path: str | Path, *,
                 clock: Callable[[], datetime] | None = None) -> None:
        self.path = Path(path)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Foreign keys stay off while an older table is copied (its self-reference moves).
        with closing(sqlite3.connect(self.path, timeout=30)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            _migrate(db)
            for statement in _DDL:
                db.execute(statement)
            db.execute("INSERT INTO review_meta(key, value) VALUES ('format', ?) "
                       "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (FORMAT,))

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        return db

    def record(self, *, tenant: str, author: Mapping[str, Any], decision: str, subject: str,
               evidence: Sequence[Mapping[str, Any]], target: str | None = None,
               link_type: str | None = None, alias: Mapping[str, str] | None = None,
               document_kind: str | None = None, retracts: str | None = None,
               allocation: Mapping[str, Any] | None = None,
               value: Mapping[str, Any] | None = None,
               identity_gate: Callable[[], Any] | None = None
               ) -> tuple[dict[str, Any], bool]:
        """Append one decision; returns ``(record, created)``. The caller has checked the
        evidence for the reader and derived ``author`` from the session. Repeating the
        decision that is already in force for its key (same payload, evidence and
        author) appends nothing and returns that record (``created`` False); one a later
        decision on its pair overrules is not in force, so repeating it records it anew.
        An ``allocate`` on a pair holding a ``reject_link`` no retraction names would
        overrule it (``_overruled``), and retracting an ``allocate`` recorded after such a
        rejection puts it back in force: checked in the write's own transaction, either is
        appended only when ``identity_gate`` (the caller's check of the author, raising
        to refuse) returns; without one, ``ReviewError`` ``identity_review_required``."""
        if decision not in DECISIONS:
            raise ReviewError("decision_invalid")
        if decision == "document_kind":   # one kind store: operating.kind_confirmation
            raise ReviewError("document_kind_is_a_confirmation")
        if not isinstance(author, Mapping) or not author.get("principal"):
            raise ReviewError("author_required")
        evidence = [dict(item) for item in evidence]
        share = reading = None
        if decision == "allocate":
            share = _allocation(allocation)
            if not target or not link_type:
                raise ReviewError("allocation_invalid")
        if decision in VALUE_DECISIONS:
            reading = _value(value)
        with closing(self._db()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if decision == "retract":
                named = db.execute("SELECT * FROM reviews WHERE tenant = ? AND review_id = ?",
                                   (tenant, retracts)).fetchone()
                if named is None:
                    raise ReviewError("review_not_found")
                if named["decision"] == "retract":
                    raise ReviewError("retract_of_a_retraction")
                if db.execute("SELECT 1 FROM reviews WHERE tenant = ? AND retracts = ?",
                              (tenant, retracts)).fetchone():
                    raise ReviewError("already_retracted")
                # Retracting an allocation that overrules a rejection puts the rejection
                # back in force: as identity-changing as the allocation itself.
                if named["decision"] == "allocate" and _rejection_held(
                        db, tenant, named["link_type"], named["subject"], named["target"],
                        before=named["seq"]):
                    _identity_checked(identity_gate)
                key, subject = named["review_key"], named["subject"]
            else:
                if retracts is not None or not evidence:
                    raise ReviewError("evidence_required" if not evidence
                                      else "retracts_only_with_retract")
                if decision == "allocate" and _rejection_held(db, tenant, link_type, subject,
                                                              target):
                    _identity_checked(identity_gate)
                key = review_key(decision, subject, target=target, link_type=link_type,
                                 alias_kind=(alias or {}).get("identifier_kind"),
                                 alias_normalized=(alias or {}).get("normalized"),
                                 allocation_property=(share or {}).get("property"),
                                 value_property=(reading or {}).get("property"))
                # In force: of its key and, for a link or allocation, of its pair.
                current = effective(_record(row) for row in db.execute(
                    "SELECT * FROM reviews WHERE tenant = ? AND review_key IN (SELECT "
                    "review_key FROM reviews WHERE tenant = ? AND (review_key = ? OR "
                    "(link_type = ? AND ((subject = ? AND target = ?) OR "
                    "(subject = ? AND target = ?))))) ORDER BY seq",
                    (tenant, tenant, key, link_type, subject, target, target,
                     subject))).get(key)
                if current is not None and current["decision"] == decision and (
                        current.get("document_kind") == document_kind
                        and current.get("allocation") == share
                        and current.get("value") == reading
                        and current["evidence"] == evidence
                        and current["author"] == dict(author)):
                    return current, False
            last = db.execute("SELECT MAX(recorded_at) FROM reviews WHERE tenant = ?",
                              (tenant,)).fetchone()[0]
            now = instant(self._clock())
            recorded_at = max(now, last) if last else now
            review_id = "rev-" + secrets.token_hex(16)
            db.execute(
                "INSERT INTO reviews (review_id, tenant, review_key, decision, subject, "
                "target, link_type, alias_kind, alias_normalized, alias_raw, document_kind, "
                "allocation_property, quantity, unit, value_property, value_text, currency, "
                "retracts, evidence, author, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (review_id, tenant, key, decision, subject, target, link_type,
                 (alias or {}).get("identifier_kind"), (alias or {}).get("normalized"),
                 (alias or {}).get("raw"), document_kind, (share or {}).get("property"),
                 (share or {}).get("quantity"), (share or reading or {}).get("unit"),
                 (reading or {}).get("property"), (reading or {}).get("value"),
                 (reading or {}).get("currency"), retracts,
                 canonical(evidence), canonical(dict(author)), recorded_at))
            row = db.execute("SELECT * FROM reviews WHERE review_id = ?",
                             (review_id,)).fetchone()
        return _record(row), True

    def get(self, tenant: str, review_id: str) -> dict[str, Any] | None:
        with closing(self._db()) as db:
            row = db.execute("SELECT * FROM reviews WHERE tenant = ? AND review_id = ?",
                             (tenant, review_id)).fetchone()
        return None if row is None else _record(row)

    def history(self, tenant: str, object_id: str) -> list[dict[str, Any]]:
        """Every record of every key that names ``object_id`` (as subject or target),
        retractions included, in store order."""
        with closing(self._db()) as db:
            rows = db.execute(
                "SELECT * FROM reviews WHERE tenant = ? AND review_key IN (SELECT review_key "
                "FROM reviews WHERE tenant = ? AND (subject = ? OR target = ?)) ORDER BY seq",
                (tenant, tenant, object_id, object_id)).fetchall()
        return [_record(row) for row in rows]

    def alias_history(self, tenant: str, keys: Iterable[tuple[str, str]] | None
                      ) -> list[dict[str, Any]]:
        """Every record of every alias key (its ``alias`` and ``reject_alias``
        decisions) whose (kind, normalized) is in ``keys`` (None: of every alias key)."""
        if keys is None:
            where, params = "", ()
        else:
            wanted = sorted({(str(kind), str(normalized)) for kind, normalized in keys})
            if not wanted:
                return []
            where = ("AND (alias_kind, alias_normalized) IN (SELECT json_extract(value, "
                     "'$[0]'), json_extract(value, '$[1]') FROM json_each(?))")
            params = (json.dumps(wanted),)
        with closing(self._db()) as db:
            rows = db.execute(
                "SELECT * FROM reviews WHERE tenant = ? AND review_key IN (SELECT review_key "
                f"FROM reviews WHERE tenant = ? AND decision IN ('alias', 'reject_alias') {where}) "
                "ORDER BY seq",
                (tenant, tenant, *params)).fetchall()
        return [_record(row) for row in rows]

    def alias_records(self, tenant: str, keys: Iterable[tuple[str, str]] | None
                      ) -> list[dict[str, Any]]:
        """``alias_history`` plus every record of each link key a derived alias of it
        names (``derived_from``), in store order: what ``alias_spans`` folds."""
        aliases = self.alias_history(tenant, keys)
        sources = sorted({r["author"].get("derived_from") for r in aliases
                          if isinstance(r["author"].get("derived_from"), str)})
        if not sources:
            return aliases
        with closing(self._db()) as db:
            rows = db.execute(
                "SELECT * FROM reviews WHERE tenant = ? AND review_key IN (SELECT review_key "
                "FROM reviews WHERE tenant = ? AND decision IN ('accept_link', 'reject_link') "
                "AND review_id IN (SELECT value FROM json_each(?))) ORDER BY seq",
                (tenant, tenant, json.dumps(sources))).fetchall()
        return sorted(aliases + [_record(row) for row in rows], key=lambda r: r["seq"])

    def kind_history(self, tenant: str) -> list[dict[str, Any]]:
        """Every record of every document kind key (recorded before kinds moved to
        ``operating.kind_confirmation``), in store order."""
        with closing(self._db()) as db:
            rows = db.execute(
                "SELECT * FROM reviews WHERE tenant = ? AND review_key IN (SELECT review_key "
                "FROM reviews WHERE tenant = ? AND decision = 'document_kind') ORDER BY seq",
                (tenant, tenant)).fetchall()
        return [_record(row) for row in rows]

    def value_history(self, tenant: str, object_id: str) -> list[dict[str, Any]]:
        """Every record of every value key of ``object_id`` (retractions included), in
        store order."""
        with closing(self._db()) as db:
            rows = db.execute(
                "SELECT * FROM reviews WHERE tenant = ? AND review_key IN (SELECT review_key "
                "FROM reviews WHERE tenant = ? AND subject = ? AND decision IN "
                "('confirm_value', 'correct_value')) ORDER BY seq",
                (tenant, tenant, object_id)).fetchall()
        return [_record(row) for row in rows]

    def value_subjects(self, tenant: str, object_ids: Iterable[str]) -> set[str]:
        """Which of ``object_ids`` have any value review (in force or not): one read, so
        an answer over many records reads ``value_history`` only for these."""
        wanted = sorted(set(object_ids))
        if not wanted:
            return set()
        with closing(self._db()) as db:
            rows = db.execute(
                "SELECT DISTINCT subject FROM reviews WHERE tenant = ? AND decision IN "
                "('confirm_value', 'correct_value') AND subject IN (SELECT value FROM "
                "json_each(?))", (tenant, json.dumps(wanted))).fetchall()
        return {row[0] for row in rows}

    def link_history(self, tenant: str) -> list[dict[str, Any]]:
        """Every record of every link and allocation key, in store order."""
        with closing(self._db()) as db:
            rows = db.execute(
                "SELECT * FROM reviews WHERE tenant = ? AND review_key IN (SELECT review_key "
                "FROM reviews WHERE tenant = ? AND decision IN "
                "('accept_link', 'reject_link', 'allocate')) ORDER BY seq",
                (tenant, tenant)).fetchall()
        return [_record(row) for row in rows]


# --------------------------------------------------------------------------- folding

def _rejection_held(db: sqlite3.Connection, tenant: str, link_type: str, subject: str,
                    target: str, *, before: int | None = None) -> bool:
    """Whether the pair holds a ``reject_link`` no retraction names (recorded before store
    sequence ``before``, when given): one an allocation on the pair overrules."""
    return db.execute(
        "SELECT 1 FROM reviews r WHERE r.tenant = ? AND r.decision = 'reject_link' AND "
        "r.link_type = ? AND ((r.subject = ? AND r.target = ?) OR (r.subject = ? AND "
        "r.target = ?)) AND (? IS NULL OR r.seq < ?) AND NOT EXISTS (SELECT 1 FROM reviews x "
        "WHERE x.tenant = r.tenant AND x.retracts = r.review_id)",
        (tenant, link_type, subject, target, target, subject, before, before)
    ).fetchone() is not None


def _identity_checked(identity_gate: Callable[[], Any] | None) -> None:
    """The caller's identity gate (raising to refuse); none refuses."""
    if identity_gate is None:
        raise ReviewError("identity_review_required")
    identity_gate()


def _by_key(records: Iterable[Mapping[str, Any]]) -> dict[str, list[Mapping[str, Any]]]:
    out: dict[str, list[Mapping[str, Any]]] = {}
    for record in sorted(records, key=lambda r: r["seq"]):
        out.setdefault(record["review_key"], []).append(record)
    return out


def _pair(record: Mapping[str, Any]) -> tuple[Any, frozenset]:
    """A link or allocation decision's link type and its two records, in either order."""
    return record.get("link_type"), frozenset((record["subject"], record.get("target")))


_OVERRULES = {"allocate": "reject_link", "reject_link": "allocate"}


def _overruled(records: Iterable[Mapping[str, Any]]
               ) -> dict[str, list[tuple[str, str | None]]]:
    """review id -> each stretch [from, until) of store time over which a later
    contradicting decision on its pair (an allocation's: a ``reject_link``; a
    rejection's: an ``allocate``) was recorded and not retracted: there the earlier one
    is not in force, whatever its own key holds. A later decision on the overruling
    one's own key does not end the stretch (an ``accept_link`` after a rejection does
    not revive the allocation before it); only its retraction does."""
    records = sorted(records, key=lambda r: r["seq"])
    retracted = {r["retracts"]: r["recorded_at"] for r in records if r["decision"] == "retract"}
    pairs: dict[tuple[Any, frozenset], list[Mapping[str, Any]]] = {}
    for record in records:
        if record["decision"] in _OVERRULES:
            pairs.setdefault(_pair(record), []).append(record)
    out: dict[str, list[tuple[str, str | None]]] = {}
    for chain in pairs.values():
        for index, earlier in enumerate(chain):
            stretches = [(later["recorded_at"], retracted.get(later["review_id"]))
                         for later in chain[index + 1:]
                         if later["decision"] == _OVERRULES[earlier["decision"]]]
            if stretches:
                out[earlier["review_id"]] = _union(stretches)
    return out


def _union(stretches: Iterable[tuple[str, str | None]]) -> list[tuple[str, str | None]]:
    """``stretches`` in order, one per start (the longest; None: open), so that
    ``held_stretches`` never compares an open end with a closed one."""
    out: dict[str, str | None] = {}
    for since, until in stretches:
        if since not in out or until is None or (out[since] is not None and until > out[since]):
            out[since] = until
    return sorted(out.items())


def _overruled_now(overruled: Mapping[str, Sequence[tuple[str, str | None]]],
                   record: Mapping[str, Any]) -> bool:
    return any(until is None for _, until in overruled.get(record["review_id"], ()))


def _accepted(record: Mapping[str, Any], by_id: Mapping[str, Mapping[str, Any]],
              chains: Mapping[str, Sequence[Mapping[str, Any]]]
              ) -> list[tuple[str, str | None]] | None:
    """For a derived alias (its author's ``derived_from`` names a link review): each
    [from, until) of store time its link's key had an acceptance in force, none when
    ``records`` do not hold that link review. None: not a derived alias."""
    source = (record.get("author") or {}).get("derived_from")
    if record["decision"] != "alias" or source is None:
        return None
    link = by_id.get(source)
    if link is None or link["decision"] not in LINK_DECISIONS:
        return []
    return [(start, until) for held, start, until in _spans(chains[link["review_key"]])
            if held["decision"] == "accept_link"]


def _overlap(begin: str, end: str | None, stretches: Sequence[tuple[str, str | None]]
             ) -> list[tuple[str, str | None]]:
    """[begin, end) within each of ``stretches``, in order."""
    out = []
    for since, until in stretches:
        start = max(begin, since)
        stop = until if end is None else end if until is None else min(end, until)
        if stop is None or start < stop:
            out.append((start, stop))
    return out


def _in_force(records: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    retracted = {r["retracts"] for r in records if r["decision"] == "retract"}
    for record in reversed(records):
        if record["decision"] != "retract" and record["review_id"] not in retracted:
            return record
    return None


def effective(records: Iterable[Mapping[str, Any]], at: str | None = None
              ) -> dict[str, Mapping[str, Any]]:
    """review key -> the decision in force (as of ``at``, store time text; None = now):
    the key's latest decision that no retraction names. A key whose decisions are all
    retracted has none, and so has one whose latest decision a later contradicting
    decision on its pair in ``records`` overrules (``_overruled``)."""
    held = [r for r in records if at is None or r["recorded_at"] <= at]
    overruled = _overruled(held)
    out = {}
    for key, chain in _by_key(held).items():
        current = _in_force(chain)
        if current is not None and not _overruled_now(overruled, current):
            out[key] = current
    return out


# ------------------------------------------------------------------ identity pin

_PO_NUMBERS = ("poNumber", "fmPoNumber", "renumberedTo")
# Where a projected purchase-order version holds the numbers ``same_po_identity``
# compares: the row fields (gold ``po_number`` and ``custom_fields``, FM and Sage
# ``ponumber``), and the link naming its supplier.
_PO_NUMBER_FIELDS = {"row.po_number": "poNumber", "row.ponumber": "poNumber",
                     "row.custom_fields.fmPoNumber": "fmPoNumber",
                     "row.custom_fields.renumberedTo": "renumberedTo"}
_PO_SUPPLIER_LINK = "PurchaseOrder.ordered_from"


def same_po_identity(cited: Mapping[str, Any], current: Mapping[str, Any]) -> bool:
    """Do two versions of one purchase-order row carry the identity a link decision pins
    (ER-09)? Each is the version's identity: ``poNumber``, ``fmPoNumber`` and
    ``renumberedTo`` (absent or None when the row has none) beside what else names the
    row (its relation, key, supplier), compared as given. Same when all agree, and on an
    in-place Portal renumber to the BC number (plx-customer-portal@5cb95e1
    portal/src/lib/business-central/align-portal-po-number.ts:49-59: the row's
    ``po_number`` becomes the BC No., ``custom_fields.renumberedTo`` that No. and
    ``fmPoNumber`` the number it had): everything but the numbers agrees, the current
    ``renumberedTo`` is the current ``poNumber`` and the current ``fmPoNumber`` is the
    cited ``poNumber`` or the cited ``fmPoNumber``. Any other number or supplier change
    is another identity."""
    cited = {k: v for k, v in cited.items() if v is not None}
    current = {k: v for k, v in current.items() if v is not None}
    if cited == current:
        return True
    if {k: v for k, v in cited.items() if k not in _PO_NUMBERS} != \
            {k: v for k, v in current.items() if k not in _PO_NUMBERS}:
        return False
    number, kept = current.get("poNumber"), current.get("fmPoNumber")
    return (number is not None and current.get("renumberedTo") == number
            and kept is not None and kept in (cited.get("poNumber"), cited.get("fmPoNumber")))


def _is_po(object_id: str) -> bool:
    return object_id.split(":", 1)[0] == "PurchaseOrder"


def _po_identity(conn: sqlite3.Connection, object_id: str, l1_handle: str
                 ) -> dict[str, Any] | None:
    """The identity (``same_po_identity``) of one projected purchase-order version: its
    numbers as the source wrote them (the identifier index's raw text, trimmed of spaces
    as Postgres ``TRIM`` does; None for a source null or a field the row lacks) and its
    supplier (the records its ``ordered_from`` link names from this version). None when
    it is not known: no projected primary number (none, or one the grammar could not
    read), two readings of one field, or no supplier link (a supplier row not projected)."""
    marks = ", ".join("?" for _ in _PO_NUMBER_FIELDS)
    found: dict[str, set[str | None]] = {}
    for path, value, raw in conn.execute(
            "SELECT v.field_path, v.value, i.raw FROM object_values v LEFT JOIN identifiers i "
            "ON i.object_id = v.object_id AND i.l1_handle = v.l1_handle "
            "AND i.field_path = v.field_path AND i.property = v.property "
            f"WHERE v.object_id = ? AND v.l1_handle = ? AND v.field_path IN ({marks})",
            (object_id, l1_handle, *_PO_NUMBER_FIELDS)):
        if value is not None and raw is None:
            return None
        found.setdefault(_PO_NUMBER_FIELDS[path], set()).add(
            None if value is None else (raw.strip(" ") or None))
    if "poNumber" not in found or any(len(texts) != 1 for texts in found.values()):
        return None
    suppliers = sorted(row[0] for row in conn.execute(
        "SELECT DISTINCT to_object FROM links WHERE from_object = ? AND link_type = ? "
        "AND binding_kind = 'fk' AND EXISTS (SELECT 1 FROM json_each(links.evidence_handles) "
        "WHERE json_each.value = ?)", (object_id, _PO_SUPPLIER_LINK, l1_handle)))
    if not suppliers:
        return None
    return {**{name: next(iter(texts)) for name, texts in found.items()},
            "supplier": tuple(suppliers)}


def version_identity(conn: sqlite3.Connection) -> Callable[[str, str], dict[str, Any] | None]:
    """``state_at``'s ``identity`` over the projection store ``conn``: the identity
    (``_po_identity``) of a purchase order's latest version with that digest, else None
    (another type, a digest not held, an identity not known)."""
    def identity(object_id: str, digest: str) -> dict[str, Any] | None:
        if not _is_po(object_id):
            return None
        row = conn.execute("SELECT l1_handle FROM object_versions WHERE object_id = ? AND "
                           "digest = ? ORDER BY recorded_at DESC LIMIT 1",
                           (object_id, digest)).fetchone()
        return None if row is None else _po_identity(conn, object_id, row[0])
    return identity


def _pinned(identity: Callable[[str, str], Mapping[str, Any] | None], object_id: str,
            cited: str, current: str) -> bool:
    """A link decision's cited purchase-order version (digest ``cited``) carries the
    identity of the current one (``current``)."""
    was, now = identity(object_id, cited), identity(object_id, current)
    return was is not None and now is not None and same_po_identity(was, now)


def state_at(records: Sequence[Mapping[str, Any]],
             current_digest: Callable[[str], str | None],
             removed: Callable[[str], bool] = lambda object_id: False,
             identity: Callable[[str, str], Mapping[str, Any] | None]
             = lambda object_id, digest: None) -> dict[str, tuple[str, str | None]]:
    """review id -> (state, reason) now. ``current_digest(object_id)``: the digest of the
    record's current version in the store the reader reads (None when not held there);
    ``removed(object_id)``: its source does not hold the record then (a removal: a
    deleted mail, a soft-deleted row).
    A decision in force whose evidence the store does not hold is ``unverified``
    (``evidence_not_in_store``); one citing a record its source removed is ``withdrawn``
    (``source_removed``), as is one whose cited evidence moved (``evidence_changed``);
    of a link decision, a cited purchase order moves only when its identity does
    (``identity(object_id, digest)``: the identity of the record's version with that
    digest in the same store, None when not known there, ``version_identity``;
    ``same_po_identity``).
    An allocation or a rejection a later contradicting decision on its pair overrules
    is ``superseded`` (``later_review``); a derived alias whose link has no acceptance in
    force now (or whose link review ``records`` do not hold) is ``withdrawn``
    (``link_not_accepted``)."""
    out: dict[str, tuple[str, str | None]] = {}
    overruled = _overruled(records)
    chains = _by_key(records)
    by_id = {r["review_id"]: r for r in records}
    for key, chain in chains.items():
        in_force = _in_force(chain)
        retracted = {r["retracts"] for r in chain if r["decision"] == "retract"}
        for record in chain:
            rid = record["review_id"]
            if record["decision"] == "retract":
                out[rid] = ("effective", None)
            elif rid in retracted:
                out[rid] = ("retracted", "retracted_by_review")
            elif in_force is None or rid != in_force["review_id"] or _overruled_now(
                    overruled, record):
                out[rid] = ("superseded", "later_review")
            elif (accepted := _accepted(record, by_id, chains)) is not None and not any(
                    until is None for _, until in accepted):
                out[rid] = ("withdrawn", "link_not_accepted")
            else:
                digests = [current_digest(item["object_id"]) for item in record["evidence"]]
                if any(d is None for d in digests):
                    out[rid] = ("unverified", "evidence_not_in_store")
                elif any(removed(item["object_id"]) for item in record["evidence"]):
                    out[rid] = ("withdrawn", "source_removed")
                elif any(d != item["digest"] and not (
                        record["decision"] in LINK_DECISIONS
                        and _pinned(identity, item["object_id"], item["digest"], d))
                        for d, item in zip(digests, record["evidence"])):
                    out[rid] = ("withdrawn", "evidence_changed")
                else:
                    out[rid] = ("effective", None)
    return out


def _spans(chain: Sequence[Mapping[str, Any]]) -> list[tuple[Mapping[str, Any], str, str | None]]:
    """(decision, from, until) for each stretch of store time one decision was in force."""
    out: list[tuple[Mapping[str, Any], str, str | None]] = []
    held: tuple[Mapping[str, Any], str] | None = None
    for index in range(len(chain)):
        moment = chain[index]["recorded_at"]
        current = _in_force(chain[:index + 1])
        if held is not None and (current is None or current["review_id"] != held[0]["review_id"]):
            out.append((held[0], held[1], moment))
            held = None
        if current is not None and held is None:
            held = (current, moment)
    if held is not None:
        out.append((held[0], held[1], None))
    return out


# --------------------------------------------------------------------------- projection

def _cited_version(conn: sqlite3.Connection, item: Mapping[str, Any], at: str
                   ) -> sqlite3.Row | None:
    """The version of the cited record with the cited digest that was current when the
    decision was made, else its latest version with that digest."""
    rows = conn.execute(
        "SELECT * FROM object_versions WHERE object_id = ? AND digest = ? "
        "ORDER BY recorded_at DESC", (item["object_id"], item["digest"])).fetchall()
    for row in rows:
        if row["recorded_at"] <= at and (row["superseded_at"] is None
                                         or row["superseded_at"] > at):
            return row
    return rows[0] if rows else None


def _cited(conn: sqlite3.Connection, record: Mapping[str, Any]
           ) -> tuple[list[sqlite3.Row], list[tuple[str, str | None]]] | None:
    """The generation's versions a decision cites (``_cited_version``) and every removal
    stretch of a cited record (``object_removals``), else None (a cited version not
    projected)."""
    versions = [_cited_version(conn, item, record["recorded_at"])
                for item in record["evidence"]]
    if None in versions:
        return None
    return versions, removal_stretches(conn, [item["object_id"] for item in record["evidence"]])


def _window(versions: Sequence[Mapping[str, Any]], removed: Sequence[tuple[str, str | None]],
            start: str, until: str | None,
            pins: Sequence[Sequence[tuple[str, str | None]]] | None = None
            ) -> list[tuple[str, str | None]] | str:
    """The stretches [begin, end) within a decision's span [start, until) over which every
    cited version is current (``pins``: per cited version, the stretches it counts as
    current, ``_identity_spans``; None: its own) and its record held by its source (none
    inside a removal), else why there is none (``source_removed_before_review``,
    ``evidence_changed_before_review``)."""
    begin = max([start] + [v["recorded_at"] for v in versions])
    spans = [(begin, until)]
    for held in pins or [[(v["recorded_at"], v["superseded_at"])] for v in versions]:
        spans = [piece for since, end in spans for piece in _overlap(since, end, held)]
    stretches = [piece for since, end in spans for piece in held_stretches(since, end, removed)]
    if stretches:
        return stretches
    if any(since <= begin and (gone is None or gone > begin) for since, gone in removed):
        return "source_removed_before_review"
    return "evidence_changed_before_review"


def _identity_spans(conn: sqlite3.Connection, version: Mapping[str, Any]
                    ) -> list[tuple[str, str | None]]:
    """[from, until) of store time over which the record of a cited version had, from
    that version on, a version carrying its identity (``same_po_identity``): what a link
    decision pins of a purchase order, as ``state_at`` reads it. The version's own span
    for another type or an identity not known."""
    object_id = version["object_id"]
    own = [(version["recorded_at"], version["superseded_at"])]
    pinned = _po_identity(conn, object_id, version["l1_handle"]) if _is_po(object_id) else None
    if pinned is None or version["superseded_at"] is None:
        return own
    out: list[tuple[str, str | None]] = []
    for handle, since, until in conn.execute(
            "SELECT l1_handle, recorded_at, superseded_at FROM object_versions "
            "WHERE object_id = ? AND recorded_at >= ? ORDER BY recorded_at",
            (object_id, version["recorded_at"])).fetchall():
        now = _po_identity(conn, object_id, handle)
        if now is None or not same_po_identity(pinned, now):
            continue
        if out and out[-1][1] == since:
            out[-1] = (out[-1][0], until)
        else:
            out.append((since, until))
    return out


def _held(conn: sqlite3.Connection, record: Mapping[str, Any], start: str, until: str | None
          ) -> tuple[list[sqlite3.Row], list[tuple[str, str | None]]] | str:
    """The generation's versions a decision cites and the stretches (``_window``) they
    held it, else why there is none (``evidence_not_projected`` or the window's reason).
    A link decision holds while each cited purchase order keeps its identity
    (``_identity_spans``)."""
    cited = _cited(conn, record)
    if cited is None:
        return "evidence_not_projected"
    pins = ([_identity_spans(conn, version) for version in cited[0]]
            if record["decision"] in LINK_DECISIONS else None)
    window = _window(cited[0], cited[1], start, until, pins)
    return window if isinstance(window, str) else (cited[0], window)


def alias_spans(records: Iterable[Mapping[str, Any]]
                ) -> list[tuple[Mapping[str, Any], str, str | None]]:
    """(alias decision, from, until) for each stretch of store time one alias decision of
    ``records`` (whole alias keys, retractions included; ``ReviewStore.alias_records``)
    was in force; a derived alias only where its link also had an acceptance in force
    (none when ``records`` lack that link's key). A ``reject_alias`` in force gives no
    stretch: a rejected alias joins nothing."""
    chains = _by_key(records)
    by_id = {r["review_id"]: r for chain in chains.values() for r in chain}
    out: list[tuple[Mapping[str, Any], str, str | None]] = []
    for chain in chains.values():
        if not any(r["decision"] in ALIAS_DECISIONS for r in chain):
            continue  # a derived alias's link key
        for record, start, until in _spans(chain):
            if record["decision"] != "alias":
                continue   # a rejection in force
            accepted = _accepted(record, by_id, chains)
            out += ([(record, start, until)] if accepted is None else
                    [(record, *stretch) for stretch in _overlap(start, until, accepted)])
    return out


def alias_rows(record: Mapping[str, Any], subject: Mapping[str, Any],
               versions: Sequence[Mapping[str, Any]],
               removed: Sequence[tuple[str, str | None]], start: str,
               until: str | None) -> list[dict[str, Any]]:
    """The identifier-index rows of one alias decision over one span [start, until): the
    one shape ``BuildAliases`` (build time) and ``ontology.resolve.ReviewedAliases``
    (``plx.find``) give. ``subject``: the subject's own index columns (``object_type``,
    and what the caller's reader needs of it); ``versions``: the cited versions (its
    ``l1_handle``, ``digest``, ``markings``, ``recorded_at``, ``superseded_at``), one per
    evidence item, in order, as the caller's gating chose them; ``removed``: the removal
    stretches of the cited records. One row per stretch of ``_window`` (a removal ends one
    and the record's return starts the next): role ``alias``, property
    ``reviewed_alias``, lineage ``workbench`` (a person's assertion), the first cited
    field's handle, path and digest, the union of the cited versions' markings; plus
    ``review_id`` and ``evidence_handles`` (every cited version). No row when the decision
    cites no field or the window is empty."""
    cited = next((i for i, item in enumerate(record["evidence"])
                  if item.get("field_path")), None)
    window = _window(versions, removed, start, until)
    if cited is None or isinstance(window, str):
        return []
    marks: set[str] = set()
    for version in versions:
        marks.update(json.loads(version["markings"]))
    alias = record["alias"]
    return [{**subject, "object_id": record["subject"],
             "identifier_kind": alias["identifier_kind"], "normalized": alias["normalized"],
             "raw": alias["raw"], "property": ALIAS_PROPERTY, "role": "alias",
             "l1_handle": versions[cited]["l1_handle"],
             "field_path": record["evidence"][cited]["field_path"], "lineage": ALIAS_LINEAGE,
             "digest": versions[cited]["digest"], "markings": markings_text(marks),
             "recorded_at": begin, "superseded_at": end, "review_id": record["review_id"],
             "evidence_handles": sorted({v["l1_handle"] for v in versions})}
            for begin, end in window]


class BuildAliases:
    """Reviewed aliases as candidates of the generation being built: the build-time
    gating of ``alias_rows`` (``ontology.resolve.ReviewedAliases`` is the per-reader one
    behind ``plx.find``). ``rows(conn, kind, normalized)`` gives, for each stretch an alias
    decision of ``tenant`` held while every version it cites (``_cited_version``) was
    current in this generation, its ``alias_rows``. Actor-free like the index: a link made
    from it cites every evidence handle, so a reader who may not read the evidence does
    not see the link. A subject the generation does not hold, or a decision citing no
    field, gives no row. ``rejected(subject, kind, normalized)``: a ``reject_alias`` of
    that alias is in force now, so the build proposes it no more. The store is read
    once."""

    def __init__(self, reviews: ReviewStore, tenant: str) -> None:
        self._spans: dict[tuple[str, str], list[tuple[Mapping[str, Any], str, str | None]]] = {}
        records = reviews.alias_records(tenant, None)
        for record, start, until in alias_spans(records):
            key = (record["alias"]["identifier_kind"], record["alias"]["normalized"])
            self._spans.setdefault(key, []).append((record, start, until))
        self._rejected = {(r["subject"], r["alias"]["identifier_kind"], r["alias"]["normalized"])
                          for r in effective(records).values()
                          if r["decision"] == "reject_alias"}
        self._rows: dict[tuple[str, str], list[dict[str, Any]]] = {}

    def rejected(self, subject: str, kind: str, normalized: str) -> bool:
        return (subject, kind, normalized) in self._rejected

    def rows(self, conn: sqlite3.Connection, kind: str, normalized: str
             ) -> list[dict[str, Any]]:
        key = (kind, normalized)
        if key not in self._rows:
            previous = conn.row_factory
            conn.row_factory = sqlite3.Row
            try:
                self._rows[key] = [row for record, start, until in self._spans.get(key, ())
                                   for row in self._rows_of(conn, record, start, until)]
            finally:
                conn.row_factory = previous
        return self._rows[key]

    @staticmethod
    def _rows_of(conn: sqlite3.Connection, record: Mapping[str, Any], start: str,
                 until: str | None) -> list[dict[str, Any]]:
        subject = conn.execute("SELECT object_type FROM objects WHERE object_id = ?",
                               (record["subject"],)).fetchone()
        cited = _cited(conn, record)
        if subject is None or cited is None:
            return []
        return alias_rows(record, {"object_type": subject[0]}, cited[0], cited[1], start,
                          until)


def alias_proposals(conn: sqlite3.Connection, reviews: ReviewStore, tenant: str
                    ) -> list[dict[str, Any]]:
    """The alias leads of a projection store (``ontology.mentions`` ``reference_alias_lead``
    features of its corroborated mention links in force now) as review proposals, one per subject and
    alias, in order: ``review`` is the ``plx.review`` record payload a person sends to
    accept it (decision ``alias``) or turn it down (``reject_alias``): subject, the alias
    as read and the field it is read in; ``state`` is ``pending``, ``accepted`` or
    ``rejected`` (the alias key's decision in force in ``reviews``); ``conflict``: the
    same alias is proposed for another subject too (a person picks, nothing is chosen
    here); ``lead_links`` and ``evidence_handles`` name the links and every handle they
    cite. Actor-free like the links: a caller shows a proposal only to a reader who may
    read every one of its ``evidence_handles`` (the corroboration included), as
    ``plx.review`` checks the cited field again when the decision is recorded."""
    found: dict[tuple[str, str, str], dict[str, Any]] = {}
    for link_id, subject, reason, handles in conn.execute(
            "SELECT link_id, to_object, reason, evidence_handles FROM links WHERE "
            "binding_kind = 'mention' AND tier = 'corroborated' AND superseded_at IS NULL "
            "AND reason LIKE ? "
            "ORDER BY link_id", ('%"reference_alias_lead"%',)):
        for lead in json.loads(reason):
            if lead.get("feature") != "reference_alias_lead":
                continue
            key = (subject, lead["identifier_kind"], lead["normalized"])
            cite = lead["citations"][0]
            entry = found.setdefault(key, {
                "review": {"mode": "record", "subject": subject,
                           "alias": {"identifier_kind": key[1], "value": lead["raw"]},
                           "evidence": [{"l1_handle": cite["l1_handle"],
                                         "digest": cite["digest"],
                                         "field_path": cite["field_path"]}]},
                "normalized": key[2], "lead_links": [], "evidence_handles": []})
            entry["lead_links"].append(link_id)
            entry["evidence_handles"] = list(dict.fromkeys(
                entry["evidence_handles"] + json.loads(handles)))
    decided = effective(reviews.alias_history(tenant, {(k, n) for _s, k, n in found}))
    subjects: dict[tuple[str, str], set[str]] = {}
    for subject, kind, normalized in found:
        subjects.setdefault((kind, normalized), set()).add(subject)
    out = []
    for (subject, kind, normalized), entry in sorted(found.items()):
        record = decided.get(review_key("alias", subject, alias_kind=kind,
                                        alias_normalized=normalized))
        state = ("pending" if record is None else
                 "accepted" if record["decision"] == "alias" else "rejected")
        out.append(dict(entry, state=state, conflict=len(subjects[(kind, normalized)]) > 1))
    return out


def link_step(reviews: ReviewStore, tenant: str
              ) -> Callable[[Any, Any], dict[str, Any]]:
    """A ``build_store`` ``post_project`` step writing ``tenant``'s link and allocation
    decisions into the generation being built (see the module docstring). Counts what
    was not written: an end or a cited version the generation does not hold, a link type
    the schema does not declare between the two types (for an allocation: many-to-many,
    in either order), evidence that changed or left its source before the decision, or
    a decision a later one on its pair overruled throughout. It runs after the other
    steps (name order) and ends their links on a pair over each stretch a rejection's
    ``contradicted`` link holds (``links_ended_by_rejection``)."""

    def step(builder: Any, projector: Any) -> dict[str, Any]:
        conn = builder.connection
        previous = conn.row_factory
        conn.row_factory = sqlite3.Row
        try:
            return _write_links(conn, builder, getattr(projector, "links", {}) or {},
                                reviews.link_history(tenant))
        finally:
            conn.row_factory = previous
    return step


def _write_links(conn: sqlite3.Connection, builder: Any, declared: Mapping[str, Any],
                 history: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    counts = {"decisions": 0, "links": 0, "allocations": 0, "end_not_projected": 0,
              "undeclared_link_type": 0, "evidence_not_projected": 0,
              "source_removed_before_review": 0, "evidence_changed_before_review": 0,
              "rejected_after_allocation": 0, "allocated_after_rejection": 0,
              "links_ended_by_rejection": 0}
    tiers: dict[str, int] = {}
    rows = []
    types: dict[str, str | None] = {}
    overruled = _overruled(history)
    refused: dict[tuple[str, str, str], list[tuple[str, str | None]]] = {}
    for chain in _by_key(history).values():
        for record, start, until in _spans(chain):
            counts["decisions"] += 1
            ends = [record["subject"], record["target"]]
            for oid in ends:
                if oid not in types:
                    found = conn.execute("SELECT object_type FROM objects WHERE object_id = ?",
                                         (oid,)).fetchone()
                    types[oid] = None if found is None else found[0]
            if None in (types[oid] for oid in ends):
                counts["end_not_projected"] += 1
                continue
            link = declared.get(record["link_type"]) or {}
            allocate = record["decision"] == "allocate"
            if allocate and link.get("cardinality") == "N:M" and (
                    link.get("from"), link.get("to")) == (types[ends[1]], types[ends[0]]):
                ends.reverse()   # stored in the declared order; the feature names the supply
            if (link.get("from"), link.get("to")) != (types[ends[0]], types[ends[1]]) or (
                    allocate and link.get("cardinality") != "N:M"):
                counts["undeclared_link_type"] += 1
                continue
            held = _held(conn, record, start, until)
            if isinstance(held, str):
                counts[held] += 1
                continue
            versions, stretches = held
            if record["review_id"] in overruled:   # the latest decision on its pair holds
                stretches = [piece for begin, end in stretches for piece
                             in held_stretches(begin, end, overruled[record["review_id"]])]
                if not stretches:
                    counts["rejected_after_allocation" if allocate
                           else "allocated_after_rejection"] += 1
                    continue
            accept = record["decision"] in ("accept_link", "allocate")
            if not accept:
                refused.setdefault((record["link_type"], ends[0], ends[1]), []).extend(
                    stretches)
            cites = [{"l1_handle": v["l1_handle"], "field_path": item["field_path"],
                      "digest": v["digest"], "lineage": v["lineage"]}
                     for v, item in zip(versions, record["evidence"]) if item.get("field_path")]
            feature: dict[str, Any] = {"feature": "person_reviewed", "agrees": accept}
            if allocate:
                feature = {"feature": ALLOCATED, "agrees": True, "supply": record["subject"],
                           **record["allocation"]}
                counts["allocations"] += 1
            if cites:
                feature["citations"] = cites[:4]
            marks: set[str] = set()
            for version in versions:
                marks.update(json.loads(version["markings"]))
            tier = "reviewed" if accept else "contradicted"
            for begin, end in stretches:   # one link per stretch its evidence was held
                rows.append({
                    "link_id": f"rvw-{record['review_id'][4:]}-{begin}",
                    "link_type": record["link_type"], "from_object": ends[0],
                    "to_object": ends[1], "tier": tier, "binding_kind": "reviewed",
                    "link_contract": None, "reason": canonical([feature]), "why_not": "[]",
                    "evidence_handles": canonical(sorted({v["l1_handle"] for v in versions})),
                    "reviewed_by": record["author"]["principal"],
                    "reviewed_at": record["recorded_at"],
                    "review_action_id": record["review_id"],
                    "markings": markings_text(marks), "recorded_at": begin,
                    "superseded_at": end})
                tiers[tier] = tiers.get(tier, 0) + 1
    counts["links_ended_by_rejection"] = _end_refused(conn, builder, refused)
    counts["links"] = builder.add("links", rows) if rows else 0
    return {"counts": counts, "link_tiers": tiers}


def _end_refused(conn: sqlite3.Connection, builder: Any,
                 refused: Mapping[tuple[str, str, str], Sequence[tuple[str, str | None]]]
                 ) -> int:
    """End, over each stretch a rejection's ``contradicted`` link holds, the links on its
    pair (link type, from, to) of a kind another ``post_project`` step wrote (a mention
    ``lead``): a rejected link is the contradicted row alone. Those kinds are written
    again by every build, an incremental one included, so ending them here never carries
    into the next build; a kind the projection writes (``fk``, ``text_match`` ...) is
    seeded forward and stays beside the contradicted row. A row cut in two keeps its id
    on its first piece; a later piece is ``<id>@<from>``. Returns the rows ended."""
    from ontology.project import _PROJECTED_LINK_KINDS
    kept = (*_PROJECTED_LINK_KINDS, "reviewed")
    ended, pieces = 0, []
    for (link_type, from_object, to_object), spans in sorted(refused.items()):
        stretches = _union(spans)
        for link in conn.execute(
                "SELECT * FROM links WHERE link_type = ? AND from_object = ? AND to_object = ? "
                f"AND binding_kind NOT IN ({', '.join('?' for _ in kept)}) ORDER BY link_id",
                (link_type, from_object, to_object, *kept)).fetchall():
            span = (link["recorded_at"], link["superseded_at"])
            held = held_stretches(*span, stretches)
            if held == [span]:
                continue
            ended += 1
            conn.execute("DELETE FROM links WHERE link_id = ?", (link["link_id"],))
            pieces += [dict(link, recorded_at=begin, superseded_at=end, link_id=(
                               link["link_id"] if index == 0 else f"{link['link_id']}@{begin}"))
                       for index, (begin, end) in enumerate(held)]
    if pieces:
        builder.add("links", pieces)
    return ended


__all__ = ["ALIAS_DECISIONS", "ALIAS_LINEAGE", "ALIAS_PROPERTY", "ALLOCATED", "BuildAliases",
           "DECISIONS", "FORMAT", "LINK_DECISIONS", "ReviewError", "ReviewStore", "STATES",
           "VALUE_DECISIONS", "alias_proposals", "alias_rows", "alias_spans", "effective",
           "link_step", "review_key", "same_po_identity", "state_at", "store_path", "version_identity"]
