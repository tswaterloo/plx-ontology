"""Live-ops cards: compute their keys from the system of record, run them as a reader, grade.

    python3 -m scripts.eval_live_ops keys --dev-dir DIR [--state DIR]
    python3 -m scripts.eval_live_ops run --dev-dir DIR --work-dir DIR --reader vince \
        [--workers 5] [--budget-seconds 6000] [--items A,B]
    python3 -m scripts.eval_live_ops rescore --dev-dir DIR --work-dir DIR --reader vince

**keys** reads, read-only (sqlite ``mode=ro``), the host state's Portal mirror
(``case-runtime/source-mirror.sqlite``: Portal, FileMaker and Sage tables), Business
Central capture (``bc-capture/capture.sqlite``), mail capture (``mail-capture/capture.db``)
and the projection's text indexes (only to confirm that nothing names a number an absence
card asks about), then asks each card's family (``evals.live_ops_keys``) for its records.
Every card of ``evals/live_ops/cards.json`` becomes ``instances`` items (``plx-eval-item/v1``,
family ``F29``, a rubric item: the runner and the base scorer run it unchanged), each with its
key (``evals.live_ops.KEY_FORMAT``: the facts, the records they are read from and the query,
``reads`` and ``rule``). The items are split by ``evals.partition.groups`` (items sharing a
record or an identifier are one group; a group is never split): groups are dealt, in the
order of their SHA-256, to held out and dev so each gets about half, and a group touching a
committed visible item (``evals.runner`` ``VISIBLE_ITEM_DIRS``, the case cards) stays dev.
Dev items and keys go to ``--dev-dir``; held-out ones to ``$PLX_EVAL_HELD_OUT_DIR/live-ops/sets/<run>`` (one per key run, ``held_out_dir``)
(the runner's held-out rule: credentialed environment, outside any git work tree). Neither
directory may be inside a git work tree: keys hold real values. Output: counts only.

**run** runs every item of both splits (or ``--items``) through ``evals.runner`` with one
headless Claude Code session each (``plx.agent.investigate``) on the persistent host as the
reader the host config names (``PLX_WORKBENCH_HOST_CONFIG``, written by
``scripts.eval_owner_host``; ``--reader`` names the profile its keys are graded for:
``vince`` full read, ``purchasing``, ``quality``), then grades each transcript against its
key (``evals.live_ops.verdict``). The report: per split and in total, correct, false,
incomplete, abstained (justified / unjustified), blocked and not applicable, and the per-card
list (card, item, split, outcome, codes: never a value) of the dev items only: a held-out
run is counted in the split, suite and family totals, never listed (``sealed``). **rescore** grades the recorded
transcripts again (a changed key rule), with no session.

Evidence label: ``live-captures`` (the reader's own host over the captured sources); the
keys are the captures' own rows, never an answer.
"""

from __future__ import annotations

import argparse
import contextlib
from collections import defaultdict
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import quote

from evals import live_ops
from evals import live_ops_keys as keys
from evals import live_ops_ceo as ceo
from evals import live_ops_inventory as inventory
from evals import live_ops_more as more
from evals import live_ops_open as open_suite
from evals import live_ops_wide as wide
from evals import partition
from evals import runner
from evals import score as scoring
from evals.oracle import po_lateness
from evals.oracle.source import canonical, instant
from ontology.mentions import own_mail_domains, sent_from_own_domain, shared_mail_domain

ROOT = Path(__file__).resolve().parents[1]
CARDS = ROOT / "evals" / "live_ops" / "cards.json"
CARDS_FORMAT = "plx-live-ops-cards/v1"
# ``rule_pending``: the rule its key follows awaits the owner; its runs are reported
# ``rule_pending`` and kept out of every rate (``evals.live_ops``).
CARD_FIELDS = frozenset({"id", "family", "instances", "facts", "question", "rule_pending",
                         "messy", "suite", "perturb", "held_share"})
MAX_MESSY = 2
REPORT_FORMAT = "plx-live-ops-report/v1"
HELD_OUT_SUBDIR = "live-ops"
MIRROR = Path("case-runtime") / "source-mirror.sqlite"
BC_CAPTURE = Path("bc-capture") / "capture.sqlite"
MAIL_CAPTURE = Path("mail-capture") / "capture.db"
PROJECTION = Path("projection") / "ontology.sqlite"
LINE_PARENT = {"salesOrderLines": "salesOrders", "salesInvoiceLines": "salesInvoices",
               "salesCreditMemoLines": "salesCreditMemos"}
# The MO line relation is wide and long: only the columns the families join on are read.
NARROW = {keys.MO: ("id", "mo_code", "status", "custom_fields"),
          keys.FM_AO: ("id", "nameamo_c", "status", "idfinprod", "datemanuf"),
          keys.FM_AO_ITEM: ("id", "idassem", "idpack", "actualamount", "qtybatch"),
          **inventory.NARROW,
          **more.NARROW, **ceo.NARROW}
TEAMS_CAPTURE = Path("teams-capture") / "capture.db"
MC_CAPTURE = Path("mc-capture") / "capture.sqlite"
SHAREPOINT_CAPTURE = Path("sharepoint-capture") / "capture.db"
# Mail domains no customer owns (a customer's domain must be its own to match its mail).
PUBLIC_DOMAINS = frozenset({"gmail.com", "hotmail.com", "outlook.com", "yahoo.com",
                            "icloud.com", "live.com", "aol.com", "msn.com", "me.com",
                            "hotmail.ca", "yahoo.ca", "rogers.com", "bell.net", "shaw.ca"})
_PO_SCAN = re.compile(keys.PO_TOKEN, re.I)
DEFAULT_WORKERS = 5
DEFAULT_BUDGET_SECONDS = 6000.0


def _ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _inside_git(path: Path) -> bool:
    return runner._git_work_tree(path.resolve()) is not None


def load_cards(path: Path = CARDS) -> tuple[list[dict[str, Any]], str]:
    raw = path.read_bytes()
    doc = json.loads(raw)
    if doc.get("format") != CARDS_FORMAT:
        raise ValueError("cards_format")
    cards = doc["cards"]
    by_id = {card.get("id"): card for card in cards}
    for card in cards:
        if card.get("perturb") is not None:
            # a perturbed card asks its base card's items: it inherits what it does not set
            base = by_id.get((card["perturb"] or {}).get("of")) if isinstance(
                card["perturb"], dict) else None
            if base is None or base.get("perturb") is not None or not set(
                    card["perturb"].get("ops") or ()) <= set(open_suite.OPS) or not \
                    card["perturb"].get("ops"):
                raise ValueError(f"card_perturb:{card.get('id')}")
            for name in ("family", "question", "messy", "facts"):
                if name in base and name not in card:
                    card[name] = base[name]
        if card.get("family") not in keys.FAMILIES:
            raise ValueError(f"card_family_unknown:{card.get('id')}")
        if set(card) - CARD_FIELDS:
            raise ValueError(f"card_field_unknown:{card.get('id')}")
        if card.get("suite", CORE_SUITE) not in SUITES:
            raise ValueError(f"card_suite_unknown:{card.get('id')}")
        if not isinstance(card.get("messy", []), list) or len(card.get("messy") or []) > MAX_MESSY:
            raise ValueError(f"card_messy:{card.get('id')}")
        if not isinstance(card.get("instances"), int) or card["instances"] < 1:
            raise ValueError(f"card_instances:{card.get('id')}")
    if len({c["id"] for c in cards}) != len(cards):
        raise ValueError("card_id_duplicate")
    return cards, hashlib.sha256(raw).hexdigest()


# --------------------------------------------------------------------------- world

def mirror_rows(db: Path, relations: Sequence[str],
                within: Mapping[str, tuple[str, Sequence[str]]] | None = None
                ) -> tuple[dict[str, list[dict]], str]:
    """relation -> ``[{"row", "citation"}]`` of the current mirror rows (``NARROW``
    relations: only their join columns; ``within``: relation -> (column, values), only the
    rows whose column holds one of the values, filtered in SQLite so a long relation is
    never held whole), and the mirror generation; read-only."""
    conn = _ro(db)
    try:
        generation = dict(conn.execute("SELECT name, value FROM mirror_meta"))["generation"]
        out: dict[str, list[dict]] = {}
        for relation in relations:
            columns = NARROW.get(relation)
            body = ("json_object(" + ", ".join(f"'{c}', json_extract(row_json, '$.{c}')"
                                               for c in columns) + ")") if columns else "row_json"
            where, args = "relation = ?", [relation]
            if within and relation in within:
                column, values = within[relation]
                where += (f" AND json_extract(row_json, '$.{column}') IN "
                          "(SELECT value FROM json_each(?))")
                args.append(json.dumps(list(values)))
            out[relation] = [
                {"row": json.loads(row), "citation":
                 f"api://{relation}/{quote(canonical(json.loads(key)), safe='')}"
                 f"@{generation}-{capture}"}
                for key, row, capture in conn.execute(
                    f"SELECT key_json, {body}, capture_id FROM row_current WHERE {where}",
                    args)]
        return out, generation
    finally:
        conn.close()


def mirror_versions(db: Path, relations: Sequence[str]
                    ) -> dict[str, dict[str, list[tuple[datetime, dict]]]]:
    """relation -> the row's citation without its capture (as ``mirror_rows`` writes it)
    -> every version the mirror captured of it (superseded and current), each with the
    UTC instant the capture that first held it finished; read-only. A version whose
    capture has no finish time is left out (never held at a time)."""
    conn = _ro(db)
    try:
        out: dict[str, dict[str, list[tuple[datetime, dict]]]] = {}
        for relation in relations:
            held = out.setdefault(relation, {})
            for key, row, finished in conn.execute(
                    "SELECT v.key_json, v.row_json, c.finished_at FROM ("
                    "SELECT key_json, row_json, capture_id FROM row_version WHERE relation = ? "
                    "UNION ALL SELECT key_json, row_json, capture_id FROM row_current "
                    "WHERE relation = ?) v JOIN capture c ON c.capture_id = v.capture_id",
                    (relation, relation)):
                at = instant(finished)
                if at is None:
                    continue
                prefix = f"api://{relation}/{quote(canonical(json.loads(key)), safe='')}"
                held.setdefault(prefix, []).append((at, json.loads(row)))
        return out
    finally:
        conn.close()


def bc_records(db: Path, sets: Sequence[str]) -> tuple[list[dict[str, Any]], str]:
    """Every current captured BC record of ``sets`` as an oracle record, read-only."""
    conn = _ro(db)
    try:
        generation = dict(conn.execute("SELECT key, value FROM meta"))["generation"]
        out = []
        for capture, unit, path, payload in conn.execute(
                "SELECT capture_id, unit, path, payload FROM versions WHERE kind = 'bc' AND "
                "superseded_at IS NULL AND missing_from_source_since IS NULL"):
            company, _, entity_set = unit.partition(":")[2].partition("/")
            if entity_set not in sets:
                continue
            fields = json.loads(payload)["row"]
            record = {"company_id": company, "entity_set": entity_set,
                      "id": str(fields.get("id")), "fields": fields,
                      "citation": f"bc://{path}@{generation}-{capture}"}
            if entity_set in LINE_PARENT:
                record.update(document_set=LINE_PARENT[entity_set],
                              document_id=fields.get("documentId"))
            out.append(record)
        return out, generation
    finally:
        conn.close()


def own_domains(conn: sqlite3.Connection) -> frozenset[str]:
    """Our own mail domains, as the app reads them (``RuntimeCitations.tenant_domains``):
    every domain of a captured mailbox's UPN and recorded addresses, less shared mail
    providers' (``ontology.mentions.own_mail_domains``)."""
    found: set[str] = set()
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND "
                        "name = 'mailboxes'").fetchone():
        return frozenset()
    for upn, identities in conn.execute("SELECT upn, identities FROM mailboxes"):
        values = [upn]
        try:
            listed = json.loads(identities or "[]")
        except ValueError:
            listed = []
        values += listed if isinstance(listed, list) else []
        found |= {str(v).rsplit("@", 1)[1] for v in values
                  if isinstance(v, str) and v.count("@") == 1}
    return own_mail_domains(found)


# Mailboxes never read by the key run (the owner's standing rule), by the local part of
# their UPN: their messages, attachments and calendars are left out of every family.
SKIPPED_MAILBOXES = frozenset({"sam", "benjamin"})


def skipped(address: Any) -> bool:
    """Whether an address (or UPN) is a skipped mailbox's (``SKIPPED_MAILBOXES``)."""
    return str(address or "").strip().casefold().split("@", 1)[0] in SKIPPED_MAILBOXES \
        and "@" in str(address or "")


class MailRefused(ValueError):
    """The mail capture cannot be read safely (a code): no mailbox list to leave the
    skipped mailboxes out by, or no people exclusion to clear senders by."""


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def skipped_mailboxes(conn: sqlite3.Connection) -> list[str]:
    """The mailbox ids of the skipped mailboxes (``SKIPPED_MAILBOXES``). Fail closed: a
    capture with no mailbox list (or messages not tied to a mailbox) cannot leave them
    out, so it is not read (``MailRefused``)."""
    if not {"mailbox_id", "upn"} <= _columns(conn, "mailboxes") \
            or "mailbox_id" not in _columns(conn, "messages"):
        raise MailRefused("mail_mailboxes_unknown")
    return [mailbox for mailbox, upn in conn.execute("SELECT mailbox_id, upn FROM mailboxes")
            if skipped(upn)]


def current_mail(conn: sqlite3.Connection) -> tuple[str, tuple[str, ...]]:
    """The WHERE clause (and its arguments) of a current captured message outside the
    skipped mailboxes (``SKIPPED_MAILBOXES``)."""
    skip = skipped_mailboxes(conn)
    return ("superseded_at IS NULL AND removed_reason IS NULL AND mailbox_id NOT IN "
            "(SELECT value FROM json_each(?))", (json.dumps(skip),))


class SenderGuard:
    """Whether a message's sender is cleared by the people exclusion (an excluded
    person's mail is never read): an address the exclusion decides is cleared unless
    excluded; one it cannot decide is cleared only outside our own domains (an excluded
    person is one of us). With no usable exclusion list nothing is cleared."""

    def __init__(self, exclusion: Any) -> None:
        self.exclusion = exclusion

    @classmethod
    def from_env(cls, environ: Mapping[str, str], state: Path) -> "SenderGuard":
        from integrations.people_exclusion import PeopleExclusion, with_directory
        return cls(with_directory(PeopleExclusion.from_env(environ), state))

    def cleared(self, sender: Any, own: Iterable[str]) -> bool:
        from integrations.live_source_guard import NotVisible
        if self.exclusion.unavailable:
            return False
        address = str(sender or "").strip().casefold()
        if not address:
            return False
        try:
            if self.exclusion.decides(address):
                return not self.exclusion.is_excluded(address)
        except NotVisible:
            return False
        domain = _domain(address)
        return bool(domain) and domain not in set(own)


def _guarded(senders: Any) -> Any:
    if senders is None:
        raise MailRefused("mail_senders_unguarded")
    return senders


def mail_entry(handle: str, message_id: str, row: Mapping[str, Any],
               internal: frozenset[str]) -> dict[str, Any]:
    """A captured message as the families read it; ``external``: the sender is not one of
    us (``own_domains``, ``sent_from_own_domain``)."""
    sender = str(row.get("from") or "")
    # The message as a citation names it (``evals.score.record_identity``).
    identity = scoring.record_identity(handle)
    return {"system": "graph-mail", "handle": handle,
            "message_id": identity[1] if identity else message_id, "from": sender,
            "to": list(row.get("to") or []), "cc": list(row.get("cc") or []),
            "subject": row.get("subject"), "preview": row.get("body_preview"),
            "received_at": row.get("received_at"), "at": row.get("received_at"),
            "sent_at": row.get("sent_at"),
            "internet_message_id": row.get("internet_message_id"),
            "conversation_id": row.get("conversation_id"),
            "external": not sent_from_own_domain(_domain(sender), internal)}


def mail_messages(db: Path, scan: "TextScan | None" = None, *, senders: Any = None
                  ) -> tuple[list[dict[str, Any]], str]:
    """The current captured messages whose subject or preview names a PO-shaped token,
    read-only, the skipped mailboxes left out (``SKIPPED_MAILBOXES``) and every sender
    ``senders`` (a ``SenderGuard``) does not clear; ``scan`` sees every other message too
    in the same pass (the wide families' text, the mined questions)."""
    senders = _guarded(senders)
    conn = _ro(db)
    try:
        meta = dict(conn.execute("SELECT key, value FROM meta"))
        internal = own_domains(conn)
        if scan is not None:
            scan.own = internal
        out = []
        where, args = current_mail(conn)
        for handle, message_id, record in conn.execute(
                "SELECT l1_handle, message_id, record FROM messages WHERE " + where, args):
            row = json.loads(record)
            if not senders.cleared(row.get("from"), internal):
                continue
            text = f"{row.get('subject') or ''}\n{row.get('body_preview') or ''}"
            entry = None
            if _PO_SCAN.search(text):
                entry = mail_entry(handle, message_id, row, internal)
                out.append(entry)
            if scan is not None:
                scan.mail(text, row, lambda: entry or mail_entry(handle, message_id, row,
                                                                internal))
        return out, str(meta.get("generation") or "")
    finally:
        conn.close()


class TextScan:
    """What the wide and open families read of the captured text beyond PO-naming mail,
    gathered in the loaders' own passes: messages naming a lot or a sales order with
    shipping words, price-change mail, and the questions asked between colleagues
    (``mined``: a line ending in "?" of an internal message naming a PO, lot or batch).
    ``own``: our mail domains (set by ``mail_messages``)."""

    def __init__(self, lots: set[str], batches: set[str], orders: set[str] = frozenset()
                 ) -> None:
        self.lots, self.batches, self.orders = lots, batches, set(orders)
        self.own: frozenset[str] = frozenset()
        self.texts: list[dict[str, Any]] = []
        self.mined: list[dict[str, Any]] = []

    def names(self, text: str) -> bool:
        """Whether a text names a PO, lot or batch (an identifier a question is about)."""
        if _PO_SCAN.search(text):
            return True
        tokens = set(re.findall(r"[A-Za-z0-9][A-Za-z0-9-]{3,}", text))
        return bool(tokens & self.lots) or bool({t.upper() for t in tokens} & self.batches)

    def wanted(self, text: str, teams: bool = False) -> bool:
        """A text a wide family may key: price-change words, a customer order with shipping
        words, a lot by its number; in Teams also an order or batch by its number."""
        if wide.PRICE.search(text):
            return True
        orders = {t.upper() for t in wide.ORDER_TOKEN.findall(text)} & self.orders
        if orders and (teams or wide.SHIPPING.search(text)):
            return True
        tokens = set(wide.LOT_TOKEN.findall(text))
        return bool(tokens & self.lots) or teams and bool(
            {t.upper() for t in tokens} & self.batches)

    def mail(self, text: str, row: Mapping[str, Any], entry: Callable[[], dict]) -> None:
        if self.wanted(text):
            self.texts.append(entry())
        if "?" in text and self.names(text):
            people = [str(row.get("from") or ""), *map(str, row.get("to") or []),
                      *map(str, row.get("cc") or [])]
            if all(_domain(p) in self.own for p in people if p) and not any(
                    skipped(p) for p in people):
                for question in open_suite.question_lines(text):
                    if self.names(question):
                        made = entry()
                        self.mined.append({"question": question, "system": "graph-mail",
                                           "handle": made["handle"], "at": made["at"],
                                           "people": people})

    def teams(self, item: Mapping[str, Any]) -> None:
        text = str(item.get("text") or "")
        if self.wanted(text, teams=True):
            self.texts.append(item)
        if "?" in text and self.names(text):
            for question in open_suite.question_lines(text):
                if self.names(question):
                    self.mined.append({"question": question, "system": item["system"],
                                       "handle": item["handle"], "at": item["at"],
                                       "people": list(item.get("people") or [])})


def text_index(db: Path) -> tuple[Callable[[str], bool], Callable[[str], bool]]:
    """(named in text, named in values) for a PO number, over the projection's indexes. The
    indexes keep no positions (``detail=none``), so the text side takes the chunks holding
    every word of the number and reads them (zlib text) for the number as a PO reference;
    the value side answers from the words alone (an over-match only withholds an absence
    card). A missing index names everything (no absence is then claimed)."""
    if not db.is_file():
        return (lambda _n: True), (lambda _n: True)
    import threading
    import zlib
    conn = _ro(db)
    lock = threading.Lock()

    def query(table: str, number: str) -> list[int]:
        words = re.findall(r"[0-9A-Za-z]+", number)
        if not words:
            return [0]
        match = " AND ".join(f'"{word}"' for word in words)
        with lock:
            return [row for (row,) in conn.execute(
                f"SELECT rowid FROM {table} WHERE {table} MATCH ? LIMIT 500", (match,))]

    def in_text(number: str) -> bool:
        digits = re.sub(r"\D", "", number)
        if not digits:
            return True
        pattern = re.compile(r"\bP\.?O\.?\s?#?\s?-?0*" + digits + r"\b", re.I)
        rows = query("chunk_text", number)
        if len(rows) >= 500:
            return True
        for chunk in rows:
            with lock:
                found = conn.execute("SELECT body FROM text_chunks WHERE chunk_id = ?",
                                     (chunk,)).fetchone()
            if found is None:
                return True
            try:
                text = zlib.decompress(found[0]).decode("utf-8", "replace")
            except zlib.error:
                return True
            if pattern.search(text):
                return True
        return False

    return in_text, (lambda number: bool(query("value_text", number)))


# A fact's records checked against the projection (the first this many).
FRESHNESS_SAMPLE = 400


def projection_versions(db: Path, mail_db: Path | None = None) -> Callable[[str], str]:
    """For a record handle: ``same`` when the projection the agent reads holds that very
    capture of the record, ``other`` when it holds another capture or lacks a record
    captured after its newest capture of that relation (created since it was built),
    ``absent`` otherwise (a relation or record it does not project); read-only.

    A mail handle carries no capture number: a message the projection lacks is ``other``
    when the mail capture (``mail_db``, default the state's ``MAIL_CAPTURE`` beside the
    projection) first recorded it after the mail capture the projection was built from
    (its ``graph_mail`` freshness), so a message mailed in since is never the agent's miss."""
    if not db.is_file():
        return lambda _handle: "absent"
    import threading
    conn = _ro(db)
    lock = threading.Lock()
    mail_db = mail_db if mail_db is not None else db.parent.parent / MAIL_CAPTURE
    mail_conn = _ro(mail_db) if mail_db.is_file() else None
    mail_cut: list[str | None] = []

    def mail_newer(handle: str) -> bool:
        if mail_conn is None:
            return False
        if not mail_cut:
            row = conn.execute("SELECT value FROM store_meta WHERE key = 'freshness'").fetchone()
            try:
                stated = json.loads(row[0]) if row else []
            except (TypeError, ValueError):
                stated = []
            mail_cut.append(next((str(f["captured_at"]) for f in stated
                                  if isinstance(f, dict) and f.get("source") == "graph_mail"
                                  and f.get("captured_at")), None))
        if mail_cut[0] is None:
            return False
        try:
            mailbox, rest = handle[len("graph-mail://"):].split("/", 1)
        except ValueError:
            return False
        first = mail_conn.execute(
            "SELECT min(recorded_at) FROM messages WHERE mailbox_id = ? AND message_id = ?",
            (mailbox, rest.split("@", 1)[0])).fetchone()
        return bool(first and first[0] and first[0] > mail_cut[0])

    projected: dict[str, int] = {}

    def check(handle: str) -> str:
        base = handle.split("#", 1)[0].rpartition("@")[0]
        if not base:
            return "absent"
        with lock:
            held = {h for (h,) in conn.execute(
                "SELECT l1_handle FROM object_versions WHERE l1_handle >= ? AND "
                "l1_handle < ? AND superseded_at IS NULL", (base + "@", base + "A"))}
            if not held:
                # a record the projection lacks is newer than it (``other``) only when it
                # was captured after every capture of its relation the projection holds; a
                # relation it never projects, or a record it simply does not hold, says
                # nothing (``absent``)
                if handle.startswith("graph-mail://"):
                    return "other" if mail_newer(handle.split("#", 1)[0]) else "absent"
                relation = base.rsplit("/", 1)[0] + "/"
                if relation not in projected:
                    projected[relation] = max(
                        (_capture_of(h) for (h,) in conn.execute(
                            "SELECT l1_handle FROM object_versions WHERE l1_handle >= ? AND "
                            "l1_handle < ?", (relation, relation[:-1] + "0"))), default=-1)
                mine = _capture_of(handle.split("#", 1)[0])
                return "other" if 0 <= projected[relation] < mine else "absent"
        return "same" if handle.split("#", 1)[0] in held else "other"
    return check


def _capture_of(handle: str) -> int:
    """The capture id a capture handle ends in (``@<generation>-<capture>``), else -1."""
    tail = handle.rpartition("@")[2].rpartition("-")[2]
    return int(tail) if tail.isdigit() else -1


def freshness_gap(facts: Sequence[Mapping[str, Any]], versions: Callable[[str], str]
                  ) -> list[str]:
    """The ids of the facts read from a capture the agent's projection does not hold (it
    holds another capture of that record): the key is newer (or older) than what the agent
    can read, so a miss there is a freshness gap, never the agent's error."""
    return [str(f.get("id")) for f in facts
            if any(versions(h) == "other" for h in (f.get("records") or ())[:FRESHNESS_SAMPLE])]


def capture_rows(db: Path, kinds: Sequence[str], scheme: Callable[[str], str],
                 keep: Callable[[Mapping[str, Any]], bool]) -> list[dict[str, Any]]:
    """Current captured records of a capture store (``integrations.capture_store``) of
    these kinds that ``keep`` admits: ``{"handle", "row"}``; read-only."""
    if not db.is_file():
        return []
    conn = _ro(db)
    try:
        generation = dict(conn.execute("SELECT key, value FROM meta"))["generation"]
        out = []
        marks = ",".join("?" for _ in kinds)
        for capture, kind, path, payload in conn.execute(
                f"SELECT capture_id, kind, path, payload FROM versions WHERE kind IN ({marks}) "
                "AND superseded_at IS NULL AND missing_from_source_since IS NULL", list(kinds)):
            row = json.loads(payload).get("row") or {}
            if keep(row):
                out.append({"handle": f"{scheme(kind)}://{path}@{generation}-{capture}",
                            "row": row})
        return out
    finally:
        conn.close()


def teams_messages(state: Path, scan: TextScan | None = None) -> list[dict[str, Any]]:
    """Current Teams chat and channel messages naming a PO; ``scan`` sees every message
    (lots, the questions asked). A chat a skipped mailbox's person is in is left out."""
    db = state / TEAMS_CAPTURE
    if not db.is_file():
        return []
    conn = _ro(db)
    out = []
    try:
        generation = dict(conn.execute("SELECT key, value FROM meta"))["generation"]
        for capture, kind, path, payload in conn.execute(
                "SELECT capture_id, kind, path, payload FROM versions WHERE kind IN "
                "('teams_chat', 'teams_channel') AND superseded_at IS NULL AND "
                "missing_from_source_since IS NULL"):
            doc = json.loads(payload)
            row = doc.get("row") or {}
            text = str(row.get("body_text") or "")
            people = list(((doc.get("meta") or {}).get("source_acl") or {}).get(
                "identities") or [])
            if not text or any(skipped(p) for p in people):
                continue
            item = {"system": kind.replace("_", "-"),
                    "handle": f"{kind.replace('_', '-')}://{path}@{generation}-{capture}",
                    "text": text, "at": row.get("created_at"), "subject": "",
                    "people": people}
            if _PO_SCAN.search(text):
                out.append(item)
            if scan is not None:
                scan.teams(item)
        return out
    finally:
        conn.close()


MEETINGS_CAPTURE = Path("meetings-capture") / "capture.db"


def calendar_events(state: Path) -> list[dict[str, Any]]:
    """Current captured calendar events (subject, preview, start, attendees and organizer
    addresses, cancelled, iCal uid); a skipped mailbox's calendar is left out."""
    db = state / MEETINGS_CAPTURE
    if not db.is_file():
        return []
    conn = _ro(db)
    out = []
    try:
        generation = dict(conn.execute("SELECT key, value FROM meta"))["generation"]
        for capture, path, payload in conn.execute(
                "SELECT capture_id, path, payload FROM versions WHERE kind = 'calendar_event' "
                "AND superseded_at IS NULL AND missing_from_source_since IS NULL"):
            doc = json.loads(payload)
            row = doc.get("row") or {}
            owners = ((doc.get("meta") or {}).get("source_acl") or {}).get("identities") or []
            if any(skipped(o) for o in owners):
                continue
            people = [a.get("address") for a in row.get("attendees") or ()
                      if isinstance(a, Mapping)]
            people.append((row.get("organizer") or {}).get("address"))
            out.append({"system": "calendar-event",
                        "handle": f"calendar-event://{path}@{generation}-{capture}",
                        "subject": row.get("subject"), "text": row.get("body_preview"),
                        "at": row.get("start"), "uid": row.get("ical_uid"),
                        "cancelled": bool(row.get("is_cancelled")),
                        "attendees": [str(p).casefold() for p in people if p]})
        return out
    finally:
        conn.close()


def attachment_names(db: Path, *, senders: Any = None) -> list[dict[str, Any]]:
    """Mail attachments whose file name names a PO-shaped token, each with its current
    message (``mail_entry``) and ``name``; the skipped mailboxes and every sender
    ``senders`` does not clear left out."""
    senders = _guarded(senders)
    if not db.is_file():
        return []
    conn = _ro(db)
    try:
        internal = own_domains(conn)
        where, args = current_mail(conn)
        named = [(message, mailbox, name) for message, mailbox, name in conn.execute(
            "SELECT message_handle, mailbox_id, json_extract(metadata, '$.name') FROM "
            "attachments" + (" WHERE mailbox_id NOT IN (SELECT value FROM json_each(?))"
                             if args else ""), args)
            if name and _PO_SCAN.search(str(name))]
        out, seen = [], {}
        for message, mailbox, name in named:
            graph_id = message.split("://", 1)[-1].split("/", 1)[-1].split("@", 1)[0]
            if (mailbox, graph_id) not in seen:
                seen[(mailbox, graph_id)] = conn.execute(
                    "SELECT l1_handle, message_id, record FROM messages WHERE mailbox_id = ? "
                    f"AND message_id = ? AND {where}", (mailbox, graph_id, *args)
                ).fetchone()
            current = seen[(mailbox, graph_id)]
            if current is not None and senders.cleared(
                    json.loads(current[2]).get("from"), internal):
                out.append({**mail_entry(current[0], current[1], json.loads(current[2]),
                                         internal), "name": name})
        return out
    finally:
        conn.close()


def mc_tasks(state: Path) -> list[dict[str, Any]]:
    return [r for r in capture_rows(state / MC_CAPTURE, ("mission_control",), lambda _k: "mc",
                                    lambda row: bool(row.get("stage") and row.get("id")))
            if "/tasks/" in r["handle"]]


def sharepoint_files(state: Path) -> list[dict[str, Any]]:
    db = state / SHAREPOINT_CAPTURE
    if not db.is_file():
        return []
    conn = _ro(db)
    try:
        return [{"handle": handle, "name": json.loads(record).get("name")}
                for handle, record in conn.execute(
                    "SELECT l1_handle, record FROM items WHERE superseded_at IS NULL AND "
                    "removed_reason IS NULL")]
    finally:
        conn.close()


def _domain(address: Any) -> str | None:
    found = re.search(r"@([\w.-]+)", str(address or ""))
    return found.group(1).strip(".").casefold() if found else None


def customer_mail(db: Path, customers: Sequence[Mapping[str, Any]], *, senders: Any = None
                  ) -> list[dict[str, Any]]:
    """Current messages from a customer's own domain (its BC customers.email, a domain no
    other customer and no shared mail provider shares), or from one of us
    (``own_domains``, ``sent_from_own_domain``: never a free-mail sender, whatever
    mailbox holds the message) to that domain, read-only. A customer
    is its name case-folded: "CITY OF MISSISSAUGA" and "City of Mississauga" are one
    customer with each card's domain, and its messages are listed under each spelling."""
    owners: dict[str, set[str]] = defaultdict(set)
    spellings: dict[str, set[str]] = defaultdict(set)
    for record in customers:
        email = str(record["fields"].get("email") or "")
        name = str(record["fields"].get("displayName") or "").strip()
        if "@" in email and name:
            owners[email.rsplit("@", 1)[-1].strip().casefold()].add(name.casefold())
            spellings[name.casefold()].add(name)
    domains = {d: next(iter(n)) for d, n in owners.items()
               if len(n) == 1 and d not in PUBLIC_DOMAINS and not shared_mail_domain(d)}
    senders = _guarded(senders)
    if not db.is_file() or not domains:
        return []
    conn = _ro(db)
    out = []
    try:
        ours = own_domains(conn)
        # sender domain, holding mailbox's domain -> one of us (the holding mailbox's
        # domain is ours too: a capture listing no mailboxes still knows its own)
        us: dict[tuple[Any, Any], bool] = {}
        where, args = current_mail(conn)
        for handle, message_id, record in conn.execute(
                "SELECT l1_handle, message_id, record FROM messages WHERE " + where, args):
            row = json.loads(record)
            if not senders.cleared(row.get("from"), ours):
                continue
            sender = _domain(row.get("from"))
            box = _domain(row.get("mailbox_upn"))
            if (sender, box) not in us:
                us[(sender, box)] = sent_from_own_domain(sender, ours | {box} - {None})
            # a mail we had with the customer: the customer wrote it, or one of us wrote it
            # to the customer. A third party's mailing that only lists a customer address
            # among its recipients (a newsletter to sixteen people) is no mail with them.
            if sender in domains:
                hit = {domains[sender]}
            elif us[(sender, box)]:
                hit = {domains[d] for d in map(_domain, re.findall(
                    r"[\w.%+-]+@[\w.-]+", f"{row.get('to') or ''} {row.get('cc') or ''}"))
                    if d in domains}
            else:
                continue
            if len(hit) != 1:
                continue
            identity = scoring.record_identity(handle)
            out.extend({"handle": handle, "message_id": identity[1] if identity else message_id,
                        "customer": name, "subject": row.get("subject"),
                        "preview": row.get("body_preview"),
                        "external": sender in domains,
                        "conversation_id": row.get("conversation_id"),
                        "from": row.get("from"), "sent_at": row.get("sent_at"),
                        "internet_message_id": row.get("internet_message_id"),
                        "at": row.get("received_at")}
                       for name in sorted(spellings[next(iter(hit))]))
        return out
    finally:
        conn.close()


def load_world(state: Path, now: datetime | None = None,
               environ: Mapping[str, str] | None = None) -> tuple[keys.World, dict[str, str]]:
    now = now or datetime.now(timezone.utc)
    environ = os.environ if environ is None else environ
    portal, mirror_gen = mirror_rows(state / MIRROR, keys.PORTAL_RELATIONS)
    bc, bc_gen = bc_records(state / BC_CAPTURE, keys.BC_SETS)
    scan = TextScan(
        {str(i["row"].get("lotnoqc") or "").strip() for i in portal.get(keys.RAW_LOT, ())
         if wide.lot_token(str(i["row"].get("lotnoqc") or "").strip())},
        {str(i["row"].get("namebatch_c") or "").strip().upper()
         for i in portal.get(keys.FM_BATCH, ()) if i["row"].get("namebatch_c")},
        {str(i["row"].get("erpcode") or "").strip().upper()
         for i in portal.get(ceo.FM_OE, ())} | {
            str(r["fields"].get("number") or "").strip().upper()
            for r in bc if r["entity_set"] == "salesOrders"})
    senders = SenderGuard.from_env(environ, state)
    mail, mail_gen = (_refusing(lambda: mail_messages(state / MAIL_CAPTURE, scan,
                                                      senders=senders), ([], ""))
                      if (state / MAIL_CAPTURE).is_file() else ([], ""))
    in_text, in_values = text_index(state / PROJECTION)
    clients = [r for r in bc if r["entity_set"] == "customers"]
    teams = teams_messages(state, scan)
    world = keys.World(as_of=po_lateness.business_today(now), now=now, portal=portal, bc=bc,
                       mail=mail, named_in_text=in_text, named_in_values=in_values,
                       teams=teams, mc=mc_tasks(state),
                       sharepoint=sharepoint_files(state),
                       customer_mail=_refusing(lambda: customer_mail(
                           state / MAIL_CAPTURE, clients, senders=senders), []),
                       portal_versions=mirror_versions(state / MIRROR, keys.PORTAL_VERSIONED),
                       texts=[*scan.texts, *calendar_events(state)],
                       attachments=_refusing(lambda: attachment_names(
                           state / MAIL_CAPTURE, senders=senders), []),
                       mined=cleared_questions(scan.mined, state, environ),
                       blind_bank=load_blind_bank(environ))
    return world, {"portal_api": mirror_gen, "bc": bc_gen, "graph_mail": mail_gen}


def _refusing(read: Callable[[], Any], empty: Any) -> Any:
    """``read()``, or ``empty`` with its code on stderr when the mail capture refuses
    (``MailRefused``): no mail is read rather than mail read unguarded."""
    try:
        return read()
    except MailRefused as refused:
        print(json.dumps({"mail_refused": str(refused)}), file=sys.stderr)
        return empty


def cleared_questions(mined: Sequence[Mapping[str, Any]], state: Path,
                      environ: Mapping[str, str]) -> list[dict[str, Any]]:
    """The mined questions whose people the people exclusion clears (sender, recipients,
    or a chat's members): every one must be decidable and not excluded. With no usable
    exclusion list nothing is cleared (fail closed): no mined question is asked."""
    from integrations.live_source_guard import NotVisible
    from integrations.people_exclusion import PeopleExclusion, is_object_id, with_directory
    guard = with_directory(PeopleExclusion.from_env(environ), state)
    if guard.unavailable:
        return []
    out = []
    for entry in mined:
        people = [p for p in entry.get("people") or () if p]
        if entry.get("system") != "graph-mail":
            # a chat's members: its object ids decide (its addresses may not all be known)
            people = [p for p in people if is_object_id(p)]
        if not people:
            continue
        try:
            if guard.excluded(people):
                continue
        except NotVisible:
            continue
        out.append({k: v for k, v in entry.items() if k != "people"})
    return out


BLIND_BANK = Path("live-ops") / "open-bank" / "blind.json"
STYLED_BANK = Path("live-ops") / "open-bank" / "blind-styles.json"
DOCS_BANK = Path("live-ops") / "open-bank" / "blind-docs.json"


def blind_bank_path(styled: Any = False) -> Path:
    """``styled``: False the plain bank, True the styled one, "docs" the documents one."""
    return runner.held_out_root() / (DOCS_BANK if styled == "docs" else
                                     STYLED_BANK if styled else BLIND_BANK)


DOC_TEXT_KINDS = re.compile(r"\.(?:xlsx?|xlsm|csv|pdf)$", re.I)
DOC_DAYS = 180
DOC_POOL = 80


def attachment_docs(db: Path, as_of: Any, *, senders: Any = None) -> list[dict[str, Any]]:
    """Recent mail attachments people work from (a spreadsheet or PDF whose name is a cost,
    inventory, count, PO or order sheet: ``evals.live_ops_open.DOC_NAME``) with their
    extracted text and current message, read-only; the skipped mailboxes and uncleared
    senders left out. At most ``DOC_POOL``, drawn by SHA-256 of the handle."""
    from integrations.captured_text import read_blob
    senders = _guarded(senders)
    if not db.is_file():
        return []
    conn = _ro(db)
    try:
        internal = own_domains(conn)
        where, args = current_mail(conn)
        named = [(message, mailbox, name, digest) for message, mailbox, name, digest in
                 conn.execute(
                     "SELECT message_handle, mailbox_id, json_extract(metadata, '$.name'), "
                     "text_sha256 FROM attachments WHERE text_status LIKE 'extracted%' AND "
                     "text_sha256 IS NOT NULL AND mailbox_id NOT IN "
                     "(SELECT value FROM json_each(?))", args)
                 if name and DOC_TEXT_KINDS.search(str(name))
                 and open_suite.DOC_NAME.search(str(name))]
        named.sort(key=lambda r: hashlib.sha256(f"{r[0]}\x1f{r[2]}".encode()).hexdigest())
        out = []
        for message, mailbox, name, digest in named:
            if len(out) >= DOC_POOL:
                break
            graph_id = message.split("://", 1)[-1].split("/", 1)[-1].split("@", 1)[0]
            current = conn.execute(
                "SELECT l1_handle, message_id, record FROM messages WHERE mailbox_id = ? "
                f"AND message_id = ? AND {where}", (mailbox, graph_id, *args)).fetchone()
            if current is None:
                continue
            row = json.loads(current[2])
            day = instant(row.get("received_at"))
            if day is None or not 0 <= (as_of - day.date()).days <= DOC_DAYS \
                    or not senders.cleared(row.get("from"), internal):
                continue
            text = read_blob(db.parent / "blobs" / "sha256", digest)
            if not text:
                continue
            out.append({**mail_entry(current[0], current[1], row, internal), "name": name,
                        "text": text.decode("utf-8", "replace")[:6000]})
        return out
    finally:
        conn.close()


def load_blind_bank(environ: Mapping[str, str]) -> list[dict[str, Any]]:
    """The blind generator's question bank (``blind``), kept beside the held-out sets: none
    when it was never made or no held-out root is set."""
    out: list[dict[str, Any]] = []
    for bank in (False, True, "docs"):
        try:
            path = blind_bank_path(bank)
        except runner.RunnerError:
            return []
        if not path.is_file():
            continue
        doc = json.loads(path.read_text("utf-8"))
        if doc.get("format") != open_suite.BLIND_FORMAT:
            continue
        out += [q for q in doc.get("questions") or ()
                if (q.get("bank") == "documents") == (bank == "docs")
                and bool(q.get("style")) == (bank is True)]
    return out


# --------------------------------------------------------------------------- items and keys

def _lineage(facts: Sequence[Mapping[str, Any]]) -> str:
    """The item's lineage stratum (informational: nothing grades on it, and the runner's
    schema keeps ``gold_native_staging`` for human-confirmed rows): the system its first
    record is read from; a Portal row as the FileMaker-era ETL most of them are."""
    first = next((h for f in facts for h in f.get("records") or ()), "")
    return ("bc_manufacturing" if first.startswith("bc://") else
            "mail" if first.startswith("graph-mail://") else
            "fm_bronze" if first.startswith(("api://public.receipt_", "api://public.generated_"))
            else "gold_etl_from_fm")


def _question_block(card: Mapping[str, Any], candidate: keys.Candidate) -> dict[str, Any]:
    """The card's question and its messy rephrasings (``messy``: 1-2, the runner's
    paraphrases; phrasing 0 is the question)."""
    block: dict[str, Any] = {"text": _question(card, candidate)}
    messy = [_asked(card, m, candidate, n + 1)
             for n, m in enumerate((card.get("messy") or [])[:MAX_MESSY])]
    if messy:
        block["paraphrases"] = messy
    return block


def _question(card: Mapping[str, Any], candidate: keys.Candidate) -> str:
    return _asked(card, card["question"], candidate, 0)


def _asked(card: Mapping[str, Any], template: str, candidate: keys.Candidate,
           phrasing: int) -> str:
    """A template asked about ``candidate``: a perturbed card's ops applied to its words
    first (``evals.live_ops_open.perturb``, seeded by the card, record and phrasing)."""
    if card.get("perturb"):
        template = open_suite.perturb(template, card["perturb"]["ops"],
                                      f"{card['id']}\x1f{candidate.identifier}\x1f{phrasing}")
    return template.format(**candidate.slots)


def build(cards: Sequence[Mapping[str, Any]], cards_sha: str, world: keys.World,
          generations: Mapping[str, str],
          versions: Callable[[str], str] | None = None
          ) -> tuple[list[dict], list[dict], list[dict]]:
    """(items, keys, per-card notes): ``instances`` candidates of each card's family, in
    rank order, each one item and its key; a card short of candidates is noted."""
    now = world.now.isoformat().replace("+00:00", "Z")
    found: dict[str, list[keys.Candidate]] = {}
    items, out_keys, notes = [], [], []
    for card in cards:
        family = card["family"]
        if family not in found:
            found[family] = keys.FAMILIES[family](world)
        picked = keys.rank(card["id"], found[family])[:card["instances"]]
        notes.append({"card": card["id"], "family": family, "wanted": card["instances"],
                      "made": len(picked)})
        for index, candidate in enumerate(picked):
            facts = [f for f in candidate.facts
                     if not card.get("facts") or f["id"] in card["facts"]]
            item_tag = f"{card['id']}#{index}"
            item = {
                "format": "plx-eval-item/v1", "id": item_tag, "family": live_ops.FAMILY,
                "split": "dev", "variant": "standard",
                "question": _question_block(card, candidate),
                "root": {"identifier": candidate.identifier[:200],
                         "identifier_kind": "live_ops_" + family,
                         "object_type": candidate.object_type},
                "as_known_at": {"time": now, "l1_generations": {"case_cards": cards_sha}},
                "closed_transaction": False, "lineage_stratum": _lineage(facts),
                "known_outcome": {"answerability": "partial", "values": [], "gaps": []},
                "must_not": ["propose_action", "invent_absent_value", "follow_injected_text"],
                "oracle": {"id": f"oracle:live-ops-{family.replace('_', '-')}@{cards_sha[:16]}",
                           "interface": "plx-eval-oracle/v1",
                           "derived_from": [f"card:live-ops/{card['id']}"],
                           "computed_at": now},
                "label": {"status": "oracle_only",
                          "note": f"live-ops card {card['id']} ({family}): graded by its "
                                  "key (evals.live_ops), never by this label"},
                "scoring": {"citations_required": True, "gap_honesty": True},
                "created_at": now}
            key = {"format": live_ops.KEY_FORMAT, "item_id": item_tag, "card": card["id"],
                   "family": family, "computed_at": now, "as_of": world.as_of.isoformat(),
                   "generations": dict(generations), "facts": facts,
                   "query": {"reads": candidate.reads, "rule": candidate.rule}}
            stale = freshness_gap(facts, versions) if versions is not None else []
            if stale:
                key["freshness_gap"] = stale
            if card.get("rule_pending"):
                key["rule_pending"] = str(card["rule_pending"])[:300]
            if card.get("suite"):
                key["suite"] = card["suite"]
            if card.get("held_share"):
                key["held_share"] = int(card["held_share"])
            items.append(item)
            out_keys.append(key)
    return items, out_keys, notes


CORE_SUITE = "core"
# ``open``: question shapes no card or route was written for; its held-out half is
# reported as counts only (``report``). The suites after the CEO set are dealt per card.
OPEN_SUITE = "open"
SUITES = (CORE_SUITE, "ceo", "wide", "proactive", "roles", OPEN_SUITE, "inventory")
PER_CARD_SUITES = frozenset({"wide", "proactive", "roles", OPEN_SUITE, "inventory"})


def _suite(key: Mapping[str, Any]) -> str:
    return str(key.get("suite") or CORE_SUITE)


def _shadow(item: Mapping[str, Any], key: Mapping[str, Any]) -> dict[str, Any]:
    """The item as ``evals.partition`` reads it: its key's records as expected citations,
    so items sharing a record or identifier are one group. The card set's digest every item
    pins is no shared source (it would make every card one group)."""
    cites = [{"l1_handle": h} for f in key["facts"] for h in f.get("records") or ()]
    # a list's members are identifiers the card answers with: a card asking about one of
    # them shares it (never a dev answer naming a held-out question's PO)
    named = " ".join(form for f in key["facts"] for member in f.get("members") or ()
                     for form in member)
    question = dict(item["question"], paraphrases=[named]) if named else item["question"]
    return {**item, "as_known_at": {}, "question": question,
            "known_outcome": {"values": [{"citations": cites}]} if cites else {}}


def split(items: list[dict], out_keys: list[dict], visible: Sequence[Mapping[str, Any]]
          ) -> None:
    """Assign each item ``dev`` or ``held_out`` by partition group (module doc) and give it
    its final id (``dev-F29-0001``, ``ho-F29-0001``), in place."""
    shadows = [_shadow(i, k) for i, k in zip(items, out_keys)]
    # ``related`` compares key digests (``partition.key_digests``), never raw keys
    seen_dev = {d for v in visible for d in partition.key_digests(v)}
    seen_held: set[str] = set()
    # Each suite is dealt on its own, the core first, so a suite added later never moves a
    # core item between splits; a later suite's group sharing a record with an earlier
    # held-out item is held out too (a dev item never shows a held-out record).
    for suite in sorted({_suite(k) for k in out_keys}, key=lambda s: (s != CORE_SUITE, s)):
        mine = [i for i, k in enumerate(out_keys) if _suite(k) == suite]
        sub = [shadows[i] for i in mine]
        touching = partition.related(sub, seen_dev)
        must_hold = partition.related(sub, seen_held)
        groups = sorted(partition.groups(sub), key=lambda g: hashlib.sha256(
            "\x1f".join(items[mine[i]]["id"] for i in g).encode()).hexdigest())
        # the core and CEO suites deal over the whole suite (their splits stay as they
        # were); a later suite deals each card on its own, so every card of it with two or
        # more groups has items on both sides
        tally: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for group in groups:
            first = out_keys[mine[group[0]]]
            card = _card_of(first) if suite in PER_CARD_SUITES else ""
            # ``held_share``: a card whose held-out half is larger (n held for each dev item)
            share = int(first.get("held_share") or 1) if suite in PER_CARD_SUITES else 1
            held, dev = tally[card]
            to_held = bool(set(group) & must_hold) or (
                held <= dev * share and not (set(group) & touching))
            for index in group:
                items[mine[index]]["split"] = "held_out" if to_held else "dev"
            tally[card] = [held + len(group), dev] if to_held else [held, dev + len(group)]
        for index in mine:
            (seen_held if items[index]["split"] == "held_out" else seen_dev).update(
                partition.key_digests(shadows[index]))
    counters = {"dev": 0, "held_out": 0}
    for item, key in zip(items, out_keys):
        counters[item["split"]] += 1
        tag = item["id"]
        item["id"] = (f"{'dev' if item['split'] == 'dev' else 'ho'}-{live_ops.FAMILY}-"
                      f"{counters[item['split']]:04d}")
        item["label"]["note"] = f"{item['label']['note']} [{tag}]"
        key["item_id"] = item["id"]


def _write_json(path: Path, value: Any) -> None:
    """An item or key file, 0600 (keys hold real values), in a 0700 directory."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=1, sort_keys=True, ensure_ascii=False)
    tmp.replace(path)


def write_split(directory: Path, items: Sequence[Mapping[str, Any]],
                out_keys: Sequence[Mapping[str, Any]]) -> None:
    for sub in ("items", "keys"):
        shutil.rmtree(directory / sub, ignore_errors=True)
    for item, key in zip(items, out_keys):
        _write_json(directory / "items" / f"{item['id']}.json", item)
        _write_json(directory / "keys" / f"{item['id']}.json", key)


def held_out_dir(dev_dir: Path) -> Path:
    """The held-out half of the key run whose dev half is ``dev_dir``: one directory per
    key run (the SHA-256 of the dev directory's path), so a later key run never overwrites
    the keys an earlier run's transcripts are graded by."""
    run = hashlib.sha256(str(dev_dir.resolve()).encode()).hexdigest()[:16]
    return runner.held_out_root() / HELD_OUT_SUBDIR / "sets" / run


def load_split(directory: Path) -> list[tuple[dict, dict]]:
    """(item, key) pairs of one split directory; a key failing ``key_problems`` refuses."""
    out = []
    for path in sorted((directory / "items").glob("*.json")):
        item = json.loads(path.read_text("utf-8"))
        key = json.loads((directory / "keys" / path.name).read_text("utf-8"))
        problems = live_ops.key_problems(key)
        if problems or key["item_id"] != item["id"]:
            raise ValueError(f"key_invalid:{item['id']}:{(problems or ['item_id'])[0]}")
        errors = runner.validate_item(item)
        if errors:
            raise ValueError(f"item_invalid:{item['id']}:{errors[0]}")
        out.append((item, key))
    return out


# --------------------------------------------------------------------------- run

def _card_of(key: Mapping[str, Any]) -> str:
    return str(key.get("card") or "")


def current_forms(key: Mapping[str, Any]) -> dict[str, Any]:
    """``key`` with each fact's ``accept``, ``contradicts`` and ``label`` replaced by today's
    vocabulary (``evals.live_ops_keys.FORMS``, ``LABELS`` by fact id); values and records
    unchanged."""
    facts = [{**fact, **keys.FORMS.get(str(fact.get("id")), {}),
              **({"label": keys.LABELS[keys.FACT_LABELS[str(fact.get("id"))]]}
                 if str(fact.get("id")) in keys.FACT_LABELS and fact.get("label") else {}),
              **({"nothing_exists": True} if fact.get("id") == "no_such_po" else {}),
              **({"unsigned": True} if (key.get("family"), fact.get("id"))
                 == ("order_margin", "cost") else {})}
             for fact in key["facts"]]
    return {**key, "facts": facts}


def run_moment(pairs: Sequence[tuple[Mapping, Mapping]]) -> datetime:
    """When the recorded run's keys were computed (one moment for the run): a rekey reads
    today's rows but judges time ("late", "since yesterday", lot windows) as of that
    moment, never today. Keys of several moments are refused."""
    moments = {str(key.get("computed_at") or "") for _item, key in pairs}
    if len(moments) != 1 or not next(iter(moments)):
        raise ValueError("rekey_needs_one_computed_at")
    return datetime.fromisoformat(next(iter(moments)).replace("Z", "+00:00"))


def rekey(pairs: Sequence[tuple[Mapping, Mapping]], world: keys.World,
          versions: Callable[[str], str] | None = None) -> list[tuple[dict, dict | None]]:
    """Recorded items with their keys computed again by today's families over ``world``:
    the candidate of the item's card family naming the item's root (None: the family no
    longer yields that record, e.g. a rule now excludes it). The agent is not run again."""
    cards = {card["id"]: card for card in load_cards()[0]}
    found: dict[str, list[keys.Candidate]] = {}
    out = []
    for item, key in pairs:
        family = key["family"]
        if family not in found:
            found[family] = keys.FAMILIES[family](world)
        card = cards.get(_card_of(key), {})
        match = next((c for c in found[family]
                      if c.identifier[:200] == item["root"]["identifier"]), None)
        if match is None:
            out.append((dict(item), None))
            continue
        facts = [f for f in match.facts if not card.get("facts") or f["id"] in card["facts"]]
        fresh = {**key, "facts": facts, "as_of": world.as_of.isoformat(),
                 "query": {"reads": match.reads, "rule": match.rule}}
        fresh.pop("freshness_gap", None)
        fresh.pop("rule_pending", None)
        if card.get("rule_pending"):
            fresh["rule_pending"] = str(card["rule_pending"])[:300]
        stale = freshness_gap(facts, versions) if versions is not None else []
        if stale:
            fresh["freshness_gap"] = stale
        out.append((dict(item), fresh))
    return out


def grade(pairs: Sequence[tuple[Mapping, Mapping]], out: Path, reader: str,
          forms: str = "recorded") -> list[dict[str, Any]]:
    """The verdict of each recorded run under ``out`` (a missing transcript: not run);
    ``forms`` ``current``: graded with today's vocabulary (``current_forms``)."""
    rows = []
    for item, key in pairs:
        if key is None:   # ``rekey``: no key today
            rows.append({"card": "", "item_id": item["id"], "split": item["split"],
                         "family": "", "outcome": "no_key", "codes": ["key_withdrawn"]})
            continue
        key = current_forms(key) if forms == "current" else key
        path = out / item["id"] / "p0-r0.transcript.json"
        base = {"card": _card_of(key), "item_id": item["id"], "split": item["split"],
                "family": key["family"], "suite": _suite(key)}
        if not path.is_file():
            rows.append({**base, "outcome": "not_run", "codes": []})
            continue
        transcript = json.loads(path.read_text("utf-8"))
        result = live_ops.verdict(item, key, transcript, reader=reader)
        rows.append({**base, "outcome": result["outcome"],
                     "justified": result.get("justified"), "codes": result["codes"],
                     "facts": [{k: f[k] for k in ("id", "stated", "evidenced", "contradicted")}
                               for f in result["facts"]],
                     "answer_ms": ((transcript.get("timing") or {}).get("answer_ms")),
                     "routed": (transcript.get("agent") or {}).get("routed", "agent")})
    return rows


def routing(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Per route (the app's profit route, fast path, agent): runs, outcomes graded the
    same way, median latency; and the share answered without a Claude session."""
    import statistics
    out: dict[str, Any] = {}
    for route in ("profit", "fast_path", "agent"):
        mine = [r for r in rows if r.get("routed", "agent") == route]
        times = [r["answer_ms"] for r in mine if isinstance(r.get("answer_ms"), (int, float))]
        outcomes: dict[str, int] = {}
        for r in mine:
            outcomes[r["outcome"]] = outcomes.get(r["outcome"], 0) + 1
        out[route] = {"runs": len(mine), "outcomes": outcomes,
                      "median_ms": round(statistics.median(times)) if times else None}
    total = len(rows)
    out["share_without_claude"] = round((out["fast_path"]["runs"] + out["profit"]["runs"])
                                        / total, 4) if total else None
    return out


# A wrong answer costs this many right ones (``scoreboard``): a guess must be right more
# than nine times in ten to score above an honest abstention, the 1% false bar's spirit.
WRONG_WEIGHT = 9
FALSE_BAR = 0.01


def scoreboard(s: Mapping[str, Any]) -> dict[str, Any]:
    """The summary ``s`` scored so a wrong answer costs more than an abstention: correct
    +1, false -``WRONG_WEIGHT``, anything else 0, over the graded runs; and whether the
    false rate is within ``FALSE_BAR``."""
    graded = s["graded"]
    score = (round((s["correct"] - WRONG_WEIGHT * s["false"]) / graded, 4)
             if graded else None)
    return {"score": score, "wrong_weight": WRONG_WEIGHT, "false_bar": FALSE_BAR,
            "false_bar_met": None if s["false_rate"] is None else s["false_rate"] <= FALSE_BAR}


def report(rows: Sequence[Mapping[str, Any]], *, reader: str, notes: Sequence[Any] = (),
           evidence_label: str, code_sha: str | None = None,
           skipped: Sequence[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """``skipped``: the key run's cards short of a keyable record (``{"card", "wanted",
    "made"}``, ``skipped.json`` beside the dev keys): counted beside the headline."""
    # An item with no key today is counted beside the rate in every summary (``no_key``,
    # outside ``graded``): a withdrawn key shrinks the denominator, never silently.
    scored = [r for r in rows if r["outcome"] != "not_run"]
    ran = [r for r in scored if r["outcome"] != "no_key"]
    totals = live_ops.summary(scored)
    # Held-out runs are counted, never listed: per split, per suite and per family only
    # their totals; no card, item or code of one reaches the report, so whoever fixes
    # failures never sees a held-out question.
    shown = [r for r in rows if not sealed(r)]
    suites = sorted({str(r.get("suite") or CORE_SUITE) for r in scored})
    per_suite = {name: {split_name: live_ops.summary(
        [r for r in scored if str(r.get("suite") or CORE_SUITE) == name
         and r["split"] == split_name]) for split_name in ("dev", "held_out")}
        for name in suites}
    by_split = {name: live_ops.summary([r for r in scored if r["split"] == name])
                for name in ("dev", "held_out")}
    short = None if skipped is None else sum(
        max(0, int(n["wanted"]) - int(n["made"])) for n in skipped)

    def line(s: Mapping[str, Any]) -> str:
        rate = "n/a" if s["correct_rate"] is None else f"{100 * s['correct_rate']:.1f}%"
        return (f"{s['correct']}/{s['graded']} correct ({rate}), {s['false']} false; "
                f"{s['no_key']} with no key, not in the denominator")
    families = sorted({str(r.get("family") or "") for r in scored})
    cards = sorted({str(r.get("card") or "") for r in scored if not sealed(r)})
    open_scored = [r for r in scored if not sealed(r)]
    return {"format": REPORT_FORMAT, "reader": reader, "evidence_label": evidence_label,
            "code_sha": code_sha, "items": len(rows),
            "headline": {"all": line(totals), **{n: line(s) for n, s in by_split.items()}},
            "no_key": sum(r["outcome"] == "no_key" for r in rows),
            "not_run": sum(r["outcome"] == "not_run" for r in rows),
            "skipped": short,
            "skipped_cards": [dict(n) for n in skipped or () if n["made"] < n["wanted"]],
            "scoreboard": {"all": scoreboard(totals),
                           **{n: scoreboard(s) for n, s in by_split.items()}},
            "totals": totals, "by_split": by_split, "per_suite": per_suite,
            "per_family": {f: live_ops.summary([r for r in scored
                                                if r.get("family") == f]) for f in families},
            "per_card": {c: live_ops.summary([r for r in open_scored if r.get("card") == c])
                        for c in cards},
            "routing": routing(ran),
            "sealed": sum(sealed(r) for r in rows),
            "cards": [{k: r.get(k) for k in ("card", "item_id", "split", "family", "outcome",
                                             "justified", "codes", "answer_ms", "routed",
                                             "suite")}
                      for r in shown], "notes": list(notes)}


def sealed(row: Mapping[str, Any]) -> bool:
    """A held-out run (any suite): counted in the totals, never listed (``report``)."""
    return row.get("split") == "held_out"


class RoutedAgent:
    """The app's routing for one question (``operator_api.backend``'s Ask), decided by the
    app's own ``ask_shortcut``: a margin question takes the app's profit route
    (``WorkspaceBackend.profit_job`` over the reader's own session: the answer Vince reads,
    its facts' cited records as the answer's values); then the deterministic fast answer
    (``operating.fast_answer``); the wrapped ``agent`` only when neither serves or both
    decline. The run records ``agent["routed"]`` (``profit``, ``fast_path`` or ``agent``)
    and, for the agent, why the routes before it did not answer (``agent["declined"]``).
    A session that cannot start goes to the agent. A list of late purchase orders takes
    the app's late-list route (``WorkspaceBackend.late_job``: the reader's gap inbox, as
    Home shows it); the app never sends it on, so neither does the scoreboard."""

    def __init__(self, agent: Any, question: str, *, session: Callable[[Any], Any] | None = None,
                 fast: Any = None) -> None:
        self.agent, self.question = agent, question
        self._session, self._fast = session, fast
        self._fast_ran = False

    @property
    def gates_final_reply(self) -> bool:   # a fast answer is gated already
        return False if self._fast_ran else bool(getattr(self.agent, "gates_final_reply", False))

    def run(self, prompt: str, host: Any) -> Any:
        declined: list[str] = []
        route = self._shortcut()
        if route == "profit":
            ran = self._profit(host, declined)
            if ran is not None:
                self._fast_ran = True
                return ran
        elif route == "late_list":
            ran = self._late(host, declined)
            if ran is not None:
                self._fast_ran = True
                return ran
        fast = self._fast
        if fast is None:
            from operating import fast_answer as fast
        ran = None
        tried: list[dict[str, Any]] = []
        if fast.serves(self.question):
            try:
                _session_host, server = self._open(host)
                why: list[str] = []
                reads = fast.Reads.of_server(server)
                tried = getattr(reads, "events", [])
                ran = fast.answer(self.question, reads, why) \
                    if self._fast is None else fast.answer(self.question, reads)
                declined += [f"fast_path:{code}" for code in why[-1:]]
            except Exception as error:  # noqa: BLE001 - any failure here: the agent answers
                declined.append(f"fast_path_unavailable:{type(error).__name__}")
                ran = None
        if ran is not None:
            self._fast_ran = True
            return runner.AgentRun(events=list(ran.events), final_text=ran.final_text,
                                   usage={}, agent={"routed": "fast_path",
                                                    "intent": ran.intent,
                                                    "final_reply": dict(ran.reply)})
        # The question's first reads, as the app's Ask makes them (``gated_run``).
        told = getattr(self.agent, "for_question", None)
        agent = told(self.question) if told is not None else self.agent
        # The app's own session limit (``operator_api.agent_ask``: ``question_budget`` under
        # the Ask's hard cap): a lookup of one named record the short one, any other
        # question the deep one, never the 60 s ``answer_budget`` of the old Ask; its hard
        # timeout is that budget, as the app's ``agent_for`` sets it. The app's front door
        # reads a lookup; here the identifier grammar does (``deep_investigation.is_lookup``,
        # as ``ClaudeCodeAgent.routed``).
        given = getattr(agent, "budget_seconds", None)
        if given is not None:
            import copy
            from operating import deep_investigation as deep
            agent = copy.copy(agent)
            agent.budget_seconds = deep.question_budget(
                self.question, given, "lookup" if deep.is_lookup(self.question) else None)
            if getattr(agent, "timeout", None) is not None:
                agent.timeout = int(agent.budget_seconds)
        result = agent.run(prompt, host)
        result.agent["routed"] = "agent"
        if declined:
            result.agent["declined"] = declined
        if tried:
            # where a declined fast answer spent its time: each call's tool and duration
            result.agent["fast_path_calls"] = [
                {"name": e.get("name"), "elapsed_ms": e.get("elapsed_ms"),
                 "is_error": bool(e.get("is_error"))} for e in tried if isinstance(e, dict)]
        return result

    def _open(self, host: Any) -> tuple[Any, Any]:
        session = self._session
        if session is None:
            from scripts import eval_real_agent as real
            session = real.agent_session
        return session(host)

    def _shortcut(self) -> str | None:
        from operator_api import backend as be
        from operator_api import models as m
        return be.ask_shortcut(m.AskRequest(question=self.question))

    def _profit(self, host: Any, declined: list[str]) -> Any:
        """The app's profit route for the question, as the reader: None when it names no
        customer or order the reader can find, or answers with a did-you-mean (the app
        then shows choices: no answer), as the app's Ask goes on from it."""
        from operating import fast_answer as fa
        from operator_api import backend as be
        from operator_api import models as m
        try:
            _session_host, server = self._open(host)
            reads = fa.Reads.of_server(server)
            view = AppView(reads)
            backend = be.WorkspaceBackend(lambda _session: view, data_label="live-captures")
            started = time.monotonic()
            request = m.AskRequest(question=self.question)
            job = backend.profit_job(request, "eval", lambda: int(
                (time.monotonic() - started) * 1000))
            payload = None if job is None else job(view)
        except Exception as error:  # noqa: BLE001 - as the app: the next route answers
            declined.append(f"profit_unavailable:{type(error).__name__}")
            return None
        if payload is None:
            declined.append("profit:no_customer")
            return None
        if payload.did_you_mean:
            # The app shows the choices and nothing else: what Vince reads is a question back.
            return runner.AgentRun(events=list(reads.events), final_text=profit_text(
                payload, reads.events), usage={}, agent={"routed": "profit",
                                                         "did_you_mean": len(payload.did_you_mean)})
        return runner.AgentRun(events=list(reads.events),
                               final_text=profit_text(payload, reads.events), usage={},
                               agent={"routed": "profit", "outcome": payload.outcome})


    def _late(self, host: Any, declined: list[str]) -> Any:
        """The app's late-list route for the question, as the reader: the late purchase
        orders of the reader's gap inbox (``WorkspaceBackend.late_job``), the answer Vince
        reads. None only when the reader's session cannot start (the app would show its
        error; the scoreboard lets the next route answer and records why)."""
        from operating import fast_answer as fa
        from operator_api import backend as be
        from operator_api import models as m
        try:
            _session_host, server = self._open(host)
            reads = fa.Reads.of_server(server)
            view = AppView(reads)
            backend = be.WorkspaceBackend(lambda _session: view, data_label="live-captures")
            started = time.monotonic()
            payload = backend.late_job(m.AskRequest(question=self.question), "eval", lambda: int(
                (time.monotonic() - started) * 1000))(view)
        except Exception as error:  # noqa: BLE001 - recorded; the next route answers
            declined.append(f"late_list_unavailable:{type(error).__name__}")
            return None
        return runner.AgentRun(events=list(reads.events),
                               final_text=late_text(payload, reads.events), usage={},
                               agent={"routed": "late_list", "outcome": payload.outcome})


class AppView:
    """A reader's workspace as the app's Ask jobs use it (``operator_ui.object_workspace``'s
    view and ``HostAccess``), over the reader's eval session (``fast_answer.Reads``): every
    tool call is the session's, recorded as a transcript event, and each cited record is
    decided for the reader by the console's own rule (``HostAccess._decide``)."""

    def __init__(self, reads: Any) -> None:
        self.reads = reads
        self.access = self
        self.actor = reads.actor
        # the store the reader's host answers from: the gap inbox is read beside it
        self.store_status = SimpleNamespace(store_path=getattr(reads.host, "store_path", None))
        self._memo: dict[str, Any] = {}

    def gap_inbox(self, *, limit: int | None = None) -> dict[str, Any]:
        """The reader's gap inbox as the app's workspace reads it
        (``ObjectWorkspace.gap_inbox``: the inbox beside the store, trimmed by this
        reader's grant and per-item decision)."""
        from operator_ui.object_workspace import ObjectWorkspace
        return ObjectWorkspace.gap_inbox(self, limit=limit)  # type: ignore[arg-type]

    def grant(self) -> tuple[Callable[[str], bool], frozenset[str]]:
        """(may this reader read an audience key, the markings it holds), from the policy
        its tool calls are decided by, as ``HostAccess.grant``; inside ``_request``."""
        reads = self.reads
        policy = reads.host.policy_now(reads._gated)
        return (lambda key: policy.may_read(reads._gated, key)), \
            frozenset(policy.held_markings(reads._gated))

    def _call(self, name: str, arguments: dict) -> dict[str, Any]:
        from operating.fast_answer import Unanswerable
        try:
            return self.reads.call(name, arguments)
        except Unanswerable:
            # the console's tools answer a refused call with its error (``_call``)
            last = self.reads.events[-1] if self.reads.events else {}
            result = last.get("result")
            return dict(result) if isinstance(result, Mapping) else {
                "error": {"code": "tool_execution_failed"}}

    def _request(self) -> Any:
        return self.reads.request()

    def _base(self) -> dict[str, Any]:
        return {}

    def _can_open(self, handle: str) -> bool:
        return False   # no console here to open it in

    def decide(self, handles: Iterable[str]) -> dict[str, Any]:
        from operator_ui.object_workspace import HostAccess
        reads = self.reads
        gated = reads._gated
        todo = [h for h in dict.fromkeys(handles) if h not in self._memo]
        if todo:
            policy = reads.host.policy_now(gated)
            item_gate = getattr(reads.host.tools, "item_gate", None)
            for handle in todo:
                self._memo[handle] = HostAccess._decide(handle, gated, policy, item_gate)
        return {h: self._memo[h] for h in handles}


def late_text(payload: Any, events: Sequence[Mapping[str, Any]]) -> str:
    """The late-list route's answer as the scoreboard grades an answer: the words Vince
    reads and, as the answer's values, each late order's name as the session served it
    (``plx.object``'s display) where a fact cites that record. Nothing the app did not show
    is added."""
    cited = {s.handle for f in payload.facts for s in f.sources}
    values: list[dict[str, Any]] = []
    seen: set[str] = set()
    for event in events:
        if event.get("name") != "plx.object" or event.get("is_error"):
            continue
        display = ((event.get("result") or {}).get("object") or {}).get("display") or {}
        origin = display.get("origin") if isinstance(display, Mapping) else None
        text = display.get("untrusted_text") if isinstance(display, Mapping) else None
        if not isinstance(origin, Mapping) or not isinstance(text, str) \
                or origin.get("citation") not in cited or origin["citation"] in seen:
            continue
        seen.add(origin["citation"])
        # named by its place in the list, never by a figure word: the grader reads a value
        # as its name and its value ("late purchase order 1" would state a late count of 1)
        values.append({"name": f"listed_purchase_order_{len(values) + 1}", "value": text,
                       "citations": [{"l1_handle": origin["citation"],
                                      "field_path": origin.get("field_path")}]})
    return app_text(payload, values)


def app_text(payload: Any, values: Sequence[Mapping[str, Any]]) -> str:
    """An app answer (``AnswerPayload``) as an answer the scoreboard grades: its direct
    answer, then each fact not already in it, with ``values`` in the answer block."""
    text = "\n".join([payload.direct_answer] + [f"- {f.text}" for f in payload.facts
                                                if f.text not in payload.direct_answer])
    block = {"format": "plx-eval-answer/v1",
             "answerability": {"answered": "answerable", "partial": "partial"}.get(
                 payload.outcome, "blocked"),
             "text": text, "values": list(values), "gaps": []}
    return text + "\n\n```json\n" + json.dumps(block, ensure_ascii=False) + "\n```"


def profit_text(payload: Any, events: Sequence[Mapping[str, Any]]) -> str:
    """The profit route's answer as the scoreboard grades an answer: the words Vince reads
    (the direct answer, then each fact) and, as the answer's values, every checked input
    (``plx.investigate`` answer entries that passed their claim check) on a record a fact
    cites as its source, cited to its field. Nothing the app did not show is added."""
    cited = {s.handle for f in payload.facts for s in f.sources}
    values: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for event in events:
        if event.get("name") != "plx.investigate" or event.get("is_error"):
            continue
        for entry in (event.get("result") or {}).get("answer") or ():
            citation = entry.get("citation") if isinstance(entry, Mapping) else None
            if entry.get("check") != "passed" or not isinstance(citation, Mapping) \
                    or citation.get("l1_handle") not in cited:
                continue
            value = entry.get("value")
            if isinstance(value, Mapping):
                value = value.get("untrusted_text", value.get("value"))
            key = (citation["l1_handle"], str(citation.get("field_path")))
            if not isinstance(value, (str, int, float)) or key in seen:
                continue
            seen.add(key)
            out = {"name": f"{entry.get('name')}_{len(values) + 1}", "value": str(value),
                   "citations": [{"l1_handle": citation["l1_handle"],
                                  "field_path": citation.get("field_path")}]}
            for extra in ("currency", "unit", "quantity_role"):
                if isinstance(entry.get(extra), str):
                    out[extra] = entry[extra]
            values.append(out)
    return app_text(payload, values)


def run_items(pairs: Sequence[tuple[Mapping, Mapping]], agent: Any,
              make_host: Callable[[], Any], *, out_dev: Path, out_held: Path | None,
              evidence_label: str, workers: int, budget_seconds: float,
              agent_only: bool = False, fast: Any = None,
              session: Callable[[Any], Any] | None = None) -> None:
    deadline = time.monotonic() + budget_seconds

    def one(pair: tuple[Mapping, Mapping]) -> None:
        item = pair[0]
        if time.monotonic() > deadline:
            return
        out = out_dev if item["split"] == "dev" else out_held
        try:
            routed = agent if agent_only else RoutedAgent(
                agent, item["question"]["text"], session=session, fast=fast)
            runner.run_items([item], routed, lambda _item: make_host(), out=out,
                             evidence_label=evidence_label)
        except runner.RunnerError as error:
            print(json.dumps({"item": item["id"], "runner_error": str(error)[:80]}),
                  file=sys.stderr)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        list(pool.map(one, pairs))


def main(argv: Sequence[str] | None = None, *, agent: Any = None,
         make_host: Callable[[], Any] | None = None,
         environ: Mapping[str, str] | None = None, world: keys.World | None = None,
         fast: Any = None, session: Callable[[Any], Any] | None = None,
         spawn: Callable[[list[str], str], str] | None = None) -> int:
    """``agent``, ``make_host``, ``world``, ``fast``, ``session``, ``spawn`` (tests): an
    injected agent, host, world, fast-answer module, session opener and blind generator."""
    from operating import deep_investigation as deep
    from scripts import eval_real_agent as real

    parser = argparse.ArgumentParser(prog="python3 -m scripts.eval_live_ops",
                                     description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    make = commands.add_parser("keys", help="compute every card's key from the sources")
    make.add_argument("--dev-dir", type=Path, required=True)
    make.add_argument("--state", type=Path, help="host state (default $PLX_WORKBENCH_STATE_DIR)")
    blind = commands.add_parser("blind", help="write the open suite's blind question bank")
    blind.add_argument("--state", type=Path, help="host state (default $PLX_WORKBENCH_STATE_DIR)")
    blind.add_argument("--count", type=int, default=40)
    blind.add_argument("--model")
    blind.add_argument("--claude-bin", default="claude")
    blind.add_argument("--timeout", type=int, default=900)
    blind.add_argument("--styles", action="store_true",
                       help="the styled bank: sloppy, dictated, mixed-language phrasings")
    blind.add_argument("--docs", action="store_true",
                       help="the documents bank: questions about a file someone attached")
    for name in ("run", "rescore"):
        sub = commands.add_parser(name)
        sub.add_argument("--dev-dir", type=Path, required=True)
        sub.add_argument("--work-dir", type=Path, required=True)
        sub.add_argument("--reader", choices=sorted(live_ops.READERS), default="vince")
        sub.add_argument("--items", default="")
        sub.add_argument("--suite", choices=("all", *SUITES), default="all",
                         help="only the cards of one suite (the core set, or the CEO set)")
        sub.add_argument("--split", choices=("dev", "held_out", "both"), default="both")
        sub.add_argument("--forms", choices=("recorded", "current"), default="recorded",
                         help="grade with each key's own words or today's (FORMS)")
        if name == "rescore":
            sub.add_argument("--rekey-state", type=Path,
                             help="host state: compute every recorded item's key again "
                                  "with today's families (rekey), then grade")
        if name == "run":
            sub.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
            sub.add_argument("--budget-seconds", type=float, default=DEFAULT_BUDGET_SECONDS)
            sub.add_argument("--timeout", type=int, default=300)
            sub.add_argument("--max-turns", type=int, default=deep.MAX_TURNS)
            sub.add_argument("--session-budget", type=float, default=deep.DEEP_SECONDS,
                             help="the Ask's hard cap (the app's ASK_SECONDS); each "
                                  "session gets question_budget under it")
            sub.add_argument("--max-steps", type=int, default=deep.MAX_STEPS)
            sub.add_argument("--claude-bin", default="claude")
            sub.add_argument("--model")
            sub.add_argument("--lookup-model",
                             help="the model a lookup of one named record runs on")
            sub.add_argument("--reader-env", default=real.READER_ENV)
            sub.add_argument("--host-config")
            sub.add_argument("--live-host-config",
                             help="the reader's own live host config (default "
                                  "$PLX_EVAL_LIVE_HOST_CONFIG): the run reads with exactly "
                                  "its grants, from a temporary copy deleted after the run")
            sub.add_argument("--offline-directory", action="store_true",
                             help="with --live-host-config: no live Entra directory read")
            sub.add_argument("--eval-principal", action="store_true")
            sub.add_argument("--agent-only", action="store_true",
                             help="send every question to the agent (skip the app's fast path)")
    args = parser.parse_args(argv)
    environ = os.environ if environ is None else environ
    with contextlib.ExitStack() as cleanup:
        return _main(args, environ, cleanup, agent=agent, make_host=make_host, world=world,
                     fast=fast, session=session, spawn=spawn)


def _main(args: argparse.Namespace, environ: Mapping[str, str],
          cleanup: contextlib.ExitStack, *, agent: Any, make_host: Any, world: Any,
          fast: Any, session: Any, spawn: Any = None) -> int:
    from scripts import eval_case_cards as cards_run
    from scripts import eval_owner_host as owner_host
    from scripts import eval_real_agent as real
    try:
        if args.command == "blind":
            return _blind(args, environ, world, spawn=spawn)
        if _inside_git(args.dev_dir):
            raise ValueError("dev_dir_inside_a_git_work_tree")
        if args.command == "keys":
            return _keys(args, environ, world)
        pairs: list[tuple[dict, dict]] = []
        if args.split in ("dev", "both"):
            pairs += load_split(args.dev_dir)
        held = held_out_dir(args.dev_dir) if args.split in ("held_out", "both") else None
        if held is not None:
            pairs += load_split(held)
        if args.suite != "all":
            pairs = [p for p in pairs if _suite(p[1]) == args.suite]
        picked = {p.strip() for p in args.items.split(",") if p.strip()}
        if picked:
            unknown = picked - {item["id"] for item, _ in pairs}
            if unknown:
                raise ValueError("items_unknown:" + ",".join(sorted(unknown))[:200])
            pairs = [p for p in pairs if p[0]["id"] in picked]
        out_dev = args.work_dir.resolve() / "dev"
        # Held-out transcripts stay under the held-out root, one directory per work dir.
        run_id = hashlib.sha256(str(args.work_dir.resolve()).encode()).hexdigest()[:16]
        out_held = held / "runs" / run_id if held else None
        injected = agent is not None
        evidence_label = "injected-transport" if injected else "live-captures"
        if args.command == "run":
            from operator_ui.session_journal import isolate_journal
            isolate_journal(args.work_dir / "journal")
            if make_host is None:
                identity, _kind, problem = cards_run.run_reader(environ, args)
                if problem is not None:
                    raise ValueError(f"reader_refused:{problem}")
                live = args.live_host_config or environ.get(owner_host.LIVE_HOST_CONFIG_ENV)
                if live:
                    # The reader's own live config, its grants unchanged; never the eval
                    # principal and never beside another config.
                    if identity is None or args.host_config:
                        raise ValueError("live_host_config_needs_a_named_reader_alone")
                    args.work_dir.mkdir(parents=True, exist_ok=True)
                    temp = cleanup.enter_context(tempfile.TemporaryDirectory(
                        prefix=".live-host-", dir=args.work_dir))
                    args.host_config = str(owner_host.from_live(
                        Path(live), Path(temp) / "config", identity,
                        offline_directory=args.offline_directory))
                make_host, part = cards_run.live_host(environ, args, identity)
                if part is not None:
                    raise ValueError(f"host_blocked:{part.get('error_code')}")
                refusal = real.reader_refusal(make_host())
                if refusal:
                    raise ValueError(f"host_refused_reader:{refusal}")
            if agent is None:
                if shutil.which(args.claude_bin) is None:
                    raise ValueError("claude_binary_not_found")
                agent = runner.ClaudeCodeAgent(
                    binary=args.claude_bin, model=args.model, timeout=args.timeout,
                    max_turns=args.max_turns, budget_seconds=args.session_budget or None,
                    max_steps=args.max_steps or None, lookup_model=args.lookup_model)
            run_items(pairs, agent, make_host, out_dev=out_dev, out_held=out_held,
                      evidence_label=evidence_label, workers=args.workers,
                      budget_seconds=args.budget_seconds, agent_only=args.agent_only, fast=fast, session=session)
        if args.command == "rescore" and args.rekey_state:
            fresh_world, _gens = load_world(args.rekey_state, now=run_moment(pairs))
            pairs = rekey(pairs, fresh_world,
                          projection_versions(args.rekey_state / PROJECTION))
        rows = grade([p for p in pairs if p[0]["split"] == "dev"], out_dev, args.reader,
                     args.forms)
        if out_held is not None:
            rows += grade([p for p in pairs if p[0]["split"] == "held_out"], out_held,
                          args.reader, args.forms)
        sha = (environ.get("PLX_EVAL_CODE_SHA") or None)
        doc = report(rows, reader=args.reader, evidence_label=evidence_label, code_sha=sha,
                     skipped=_skipped(args.dev_dir, args.suite))
    except (runner.RunnerError, live_ops.KeyProblem, scoring.ScoreError, ValueError,
            OSError) as exc:
        print(json.dumps({"error": type(exc).__name__, "code": str(exc)[:200]}),
              file=sys.stderr)
        return 1
    print(json.dumps(doc, indent=1, sort_keys=True))
    return 0


def _keys(args: argparse.Namespace, environ: Mapping[str, str], world: keys.World | None
          ) -> int:
    cards, cards_sha = load_cards()
    generations: dict[str, str] = {}
    world_loaded, named = world is None, None
    if world is None:
        named = args.state or environ.get(real_state_env())
        state = Path(named) if named else None
        if state is None or not state.is_dir():
            raise ValueError("state_missing")
        world, generations = load_world(state)
    versions = (projection_versions(Path(named) / PROJECTION) if world_loaded and named
                else None)
    items, out_keys, notes = build(cards, cards_sha, world, generations, versions)
    visible = [item for root in runner.VISIBLE_ITEM_DIRS if root.is_dir()
               for item in runner.load_items(root, split="dev", admit=False)]
    split(items, out_keys, visible + runner.visible_cards())
    held = held_out_dir(args.dev_dir)
    pairs = list(zip(items, out_keys))
    write_split(args.dev_dir, [i for i, _ in pairs if i["split"] == "dev"],
                [k for i, k in pairs if i["split"] == "dev"])
    write_split(held, [i for i, _ in pairs if i["split"] == "held_out"],
                [k for i, k in pairs if i["split"] == "held_out"])
    suites = {card["id"]: card.get("suite", CORE_SUITE) for card in cards}
    # counts only (no record): what each card wanted and how many records it could key
    _write_json(args.dev_dir / SKIPPED, [{**n, "suite": suites[n["card"]]} for n in notes])
    per_suite: dict[str, dict[str, int]] = {}
    for item, key in pairs:
        counts = per_suite.setdefault(_suite(key), {"dev": 0, "held_out": 0})
        counts[item["split"]] += 1
    print(json.dumps({"cards": len(cards), "items": len(items),
                      "dev": sum(i["split"] == "dev" for i in items),
                      "held_out": sum(i["split"] == "held_out" for i in items),
                      "per_suite": per_suite,
                      "as_of": world.as_of.isoformat(),
                      "short_cards": [n for n in notes if n["made"] < n["wanted"]],
                      "generations": generations}, indent=1, sort_keys=True))
    return 0


SKIPPED = "skipped.json"


def _blind(args: argparse.Namespace, environ: Mapping[str, str], world: keys.World | None,
           spawn: Callable[..., str] | None = None) -> int:
    """Write the blind question bank (``evals.live_ops_open``): a sample of real records
    and their linked text, ``BLIND_PROMPT`` and nothing else (no card, family or route)
    go to one headless Claude Code call; its questions, each with the record fields that
    answer it, are kept beside the held-out sets (0600). Prints counts only: the bank's
    questions become dev and held-out items and are never shown."""
    import subprocess
    from operating.agent_environment import agent_environment
    if world is None:
        named = args.state or environ.get(real_state_env())
        if not named or not Path(named).is_dir():
            raise ValueError("state_missing")
        world, _gens = load_world(Path(named), environ=environ)
    styled = bool(getattr(args, "styles", False))
    docs = bool(getattr(args, "docs", False))
    path = blind_bank_path("docs" if docs else styled)
    seed = world.as_of.isoformat() + ("/styles" if styled else "/docs" if docs else "")
    figures = open_suite.aggregates(world) if styled else []
    documents: list[dict[str, Any]] = []
    if docs:
        named = args.state or environ.get(real_state_env())
        found = attachment_docs(Path(named) / MAIL_CAPTURE, world.as_of,
                                senders=SenderGuard.from_env(environ, Path(named))) \
            if named else []
        documents, sample = open_suite.doc_sample(world, found, seed)
    else:
        sample = open_suite.sample_records(world, seed)
    prompt = open_suite.blind_prompt(sample, args.count, figures, styled=styled,
                                     documents=documents)
    argv = [args.claude_bin, "-p", "--output-format", "json", "--setting-sources", "",
            "--tools", "", "--no-session-persistence"] + (
        ["--model", args.model] if args.model else [])
    if spawn is None:
        out = subprocess.run(argv, env=agent_environment(dict(environ)), input=prompt,
                             capture_output=True, text=True, timeout=args.timeout,
                             check=False).stdout
    else:
        out = spawn(argv, prompt)
    try:
        text = json.loads(out).get("result") or ""
        reply = json.loads(re.search(r"\{.*\}", text, re.S).group(0))
    except (ValueError, AttributeError, TypeError):
        raise ValueError("blind_reply_unreadable") from None
    bank = open_suite.blind_bank(reply, sample, made_at=world.now.isoformat(),
                                 model=args.model, figures=figures, documents=documents)
    _write_json(path, bank)
    print(json.dumps({"sampled": len(sample), "documents": len(documents),
                      "questions": len(bank["questions"]), "asked_for": args.count},
                     sort_keys=True))
    return 0


def _skipped(dev_dir: Path, suite: str = "all") -> list[dict[str, Any]] | None:
    """The key run's per-card counts (``SKIPPED``, written by ``keys``): None when the key
    run predates it (nothing is claimed about skipped cards then)."""
    path = dev_dir / SKIPPED
    if not path.is_file():
        return None
    rows = json.loads(path.read_text("utf-8"))
    return [r for r in rows if suite == "all" or r.get("suite", CORE_SUITE) == suite]


def real_state_env() -> str:
    from scripts import eval_real_agent as real
    return real.STATE_ENV


if __name__ == "__main__":
    raise SystemExit(main())
