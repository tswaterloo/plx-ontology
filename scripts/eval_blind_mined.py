"""Build the blind mined eval family (``evals.blind_mined``) from the captured stores.

    python3 -m scripts.eval_blind_mined build --dev-dir DIR [--state DIR] [--dev-share 0.15]
        [--show-dev]

**build** reads, read-only (sqlite ``mode=ro``) and never through a live source:

* the mail capture (``mail-capture/capture.db``): current messages outside the owner's
  skipped mailboxes (``scripts.eval_live_ops.current_mail``), whose sender, recipients and
  copies are all ours (internal mail between colleagues) and none a skipped mailbox's,
  each sender cleared by the people exclusion (``SenderGuard``);
* the Teams capture (``teams-capture/capture.db``): current chat and channel messages
  (``scripts.eval_live_ops.teams_messages``: a chat a skipped mailbox's person is in is
  left out) whose members are all ours;
* every mined question's people then cleared by the people exclusion as the host reads it
  (``PLX_EXCLUDED_PEOPLE_OIDS`` / ``PLX_EXCLUDED_PEOPLE_FILE`` with the captured
  directory: ``scripts.eval_live_ops.cleared_questions``). With no usable exclusion list
  the build refuses (fail closed): no question is mined;
* the published projection generation (``projection/CURRENT``, its ``ontology.sqlite``):
  the identifiers a question names and the record's current system-of-record values.

Each question is answered by ``evals.blind_mined.answer`` (dropped when it cannot be
settled), made standalone with a date anchor (the generation's day) and perturbed by the
general procedures (``PERTURBATIONS``). Items sharing a record are one group
(``evals.partition``); groups are dealt so about ``--dev-share`` of the items are dev, a
group touching a committed visible item staying dev. Dev items and keys go to
``--dev-dir``; held-out ones to the live-ops held-out set of that dev dir
(``scripts.eval_live_ops.held_out_dir``), so ``scripts.eval_live_ops run --dev-dir DIR``
runs both and reports the held-out share as totals only. Neither directory may be inside
a git work tree (keys hold real values). Output: counts only; ``--show-dev`` adds the dev
items' questions (the visible slice).
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any, Iterable, Mapping, Sequence

from evals import blind_mined as bm
from evals import partition
from evals import runner
from evals.live_ops_open import scoring_echo
from scripts import eval_live_ops as lo

PROJECTION_DIR = Path("projection")
TEAMS_SYSTEMS = ("teams-chat", "teams-channel")
# Object types that are text, never a record a question is answered from.
TEXT_TYPES = frozenset({"Email", "Attachment", "TeamsChatMessage", "TeamsChannelMessage",
                        "SharePointFile", "SharePointListItem", "ContainerMember",
                        "CalendarEvent", "Meeting", "MCTask", "MCTaskEvent", "PlannerTask",
                        "KnowledgeEntry", "DecisionRecord"})
MAX_OBJECTS = 12
MAX_NEIGHBOURS = 20
MAX_SIBLINGS = 5000
FIRM = "('verified', 'reviewed', 'corroborated')"
# What a perturbed item may be: at most this many per base question.
PERTURBED_PER_BASE = 3
# A line with no code and no pronoun is about its thread's record only when this short.
SUBJECT_WORDS = 7
MAX_PER_ASK = 30
DEFAULT_DEV_SHARE = 0.15


class Refused(ValueError):
    """The build cannot run safely (a code, never a value)."""


# --------------------------------------------------------------------------- mining

def _people_domain(value: Any) -> str | None:
    text = str(value or "").strip().casefold()
    return text.rsplit("@", 1)[1] if text.count("@") == 1 else None


def mail_questions(db: Path, senders: lo.SenderGuard) -> list[dict[str, Any]]:
    """The questions of internal mail (every party ours, none a skipped mailbox's, the
    sender cleared), from each current message's preview; read-only."""
    conn = lo._ro(db)
    try:
        internal = lo.own_domains(conn)
        if not internal:
            raise Refused("mail_own_domains_unknown")
        where, args = lo.current_mail(conn)
        out = []
        for handle, sender, to, cc, subject, preview, at in conn.execute(
                "SELECT l1_handle, json_extract(record, '$.from'), json_extract(record, '$.to'), "
                "json_extract(record, '$.cc'), json_extract(record, '$.subject'), "
                "json_extract(record, '$.body_preview'), json_extract(record, '$.received_at') "
                "FROM messages WHERE " + where +
                " AND instr(json_extract(record, '$.body_preview'), '?') > 0", args):
            people = [str(sender or "")] + [str(p) for p in json.loads(to or "[]")] + \
                [str(p) for p in json.loads(cc or "[]")]
            if not all(p and _people_domain(p) in internal for p in people) \
                    or any(lo.skipped(p) for p in people) \
                    or not senders.cleared(sender, internal):
                continue
            for question in bm.question_lines(str(preview or "")):
                if bm.topical(question):
                    out.append({"question": question, "system": "graph-mail",
                                "handle": handle, "at": at, "subject": str(subject or ""),
                                "people": people})
        return out
    finally:
        conn.close()


class _TeamsScan:
    """``scripts.eval_live_ops.teams_messages``'s scan: every current message it lets
    through (skipped mailboxes' chats already left out)."""

    def __init__(self, internal: frozenset[str]) -> None:
        self.internal = internal
        self.found: list[dict[str, Any]] = []

    def teams(self, item: Mapping[str, Any]) -> None:
        people = [str(p) for p in item.get("people") or () if p]
        addresses = [p for p in people if "@" in p]
        if not people or not all(_people_domain(p) in self.internal for p in addresses):
            return
        for question in bm.question_lines(str(item.get("text") or "")):
            if bm.topical(question):
                self.found.append({"question": question, "system": item["system"],
                                   "handle": item["handle"], "at": item.get("at"),
                                   "subject": "", "people": people})


def teams_questions(state: Path, internal: frozenset[str]) -> list[dict[str, Any]]:
    scan = _TeamsScan(internal)
    lo.teams_messages(state, scan)   # type: ignore[arg-type]
    return scan.found


def mined(state: Path, environ: Mapping[str, str]) -> list[dict[str, Any]]:
    """Every cleared mined question (mail and Teams), deduplicated by its words."""
    from integrations.people_exclusion import PeopleExclusion, with_directory
    guard = with_directory(PeopleExclusion.from_env(environ), state)
    if guard.unavailable:
        raise Refused(f"people_exclusion_unavailable:{guard.unavailable}")
    mail_db = state / lo.MAIL_CAPTURE
    if not mail_db.is_file():
        raise Refused("mail_capture_missing")
    senders = lo.SenderGuard(guard)
    found = mail_questions(mail_db, senders)
    conn = lo._ro(mail_db)
    try:
        internal = lo.own_domains(conn)
    finally:
        conn.close()
    found += teams_questions(state, internal)
    cleared = lo.cleared_questions(found, state, environ)
    seen: set[str] = set()
    out = []
    for entry in sorted(cleared, key=lambda e: (str(e.get("at") or ""), e["handle"])):
        words = re.sub(r"\W+", " ", entry["question"].casefold()).strip()
        if words in seen:
            continue
        seen.add(words)
        out.append(entry)
    return out


# --------------------------------------------------------------------------- projection

def current_generation(state: Path) -> tuple[Path, str, date]:
    """(the published generation's store, its generation, its day)."""
    doc = json.loads((state / PROJECTION_DIR / "CURRENT").read_text("utf-8"))
    generation = str(doc["generation"])
    store = state / PROJECTION_DIR / "generations" / generation / "store.sqlite"
    if not store.is_file():
        store = state / lo.PROJECTION
    committed = datetime.fromisoformat(str(doc["committed_at"]).replace("Z", "+00:00"))
    return store, generation, committed.date()


class Projection:
    """Read-only lookups over the projection store: identifiers and current values."""

    def __init__(self, store: Path) -> None:
        self.conn = lo._ro(store)
        self.conn.execute("PRAGMA query_only = 1")
        self._binding: dict[str, list[tuple[str, str, str]]] = {}

    def close(self) -> None:
        self.conn.close()

    def resolve(self, code: bm.Code) -> tuple[str, str, list[tuple[str, str]]] | None:
        """(object type, the code as stored, [(object id, binding)]) of the one record type
        the code names as a primary identifier of an allowed kind; None when it names none
        or records of two types."""
        forms = bm.lookup_forms(code.text)
        kinds = bm.kinds_for(code.word)
        if not forms:
            return None
        rows = self.conn.execute(
            "SELECT DISTINCT i.object_id, o.object_type, o.binding_id, i.raw, "
            "i.identifier_kind, i.role FROM identifiers i "
            "JOIN objects o ON o.object_id = i.object_id WHERE i.normalized IN "
            f"({','.join('?' * len(forms))}) AND i.superseded_at IS NULL "
            f"AND i.identifier_kind IN ({','.join('?' * len(kinds))}) LIMIT 200",
            (*forms, *kinds)).fetchall()
        rows = [r for r in rows if r[1] not in TEXT_TYPES]
        object_type = bm.pick(code, [(r[1], r[4], r[5], r[3]) for r in rows])
        rows = [(*r[:3], str(r[3]).strip(), *r[4:]) for r in rows if r[1] == object_type]
        if object_type is None or len({r[0] for r in rows}) > MAX_OBJECTS:
            return None
        raws = sorted({r[3] for r in rows if r[4] != "fm_id"} or {r[3] for r in rows},
                      key=lambda raw: (raw.upper() != code.text.upper(), -len(raw), raw))
        return object_type, raws[0], sorted({(r[0], r[2]) for r in rows})

    def values(self, objects: Sequence[tuple[str, str]], object_type: str
               ) -> list[dict[str, Any]]:
        ids = [o for o, _b in objects]
        rows = self.conn.execute(
            "SELECT object_id, property, value_type, value, l1_handle, lineage, markings "
            f"FROM object_values WHERE object_id IN ({','.join('?' * len(ids))}) "
            "AND superseded_at IS NULL", ids).fetchall()
        return [{"object_id": r[0], "object_type": object_type, "property": r[1],
                 "value_type": r[2], "value": r[3], "l1_handle": r[4], "lineage": r[5],
                 "markings": r[6]} for r in rows]

    def neighbours(self, objects: Sequence[tuple[str, str]], types: Iterable[str]
                   ) -> list[dict[str, Any]]:
        """The current values of the records of ``types`` firmly linked to the record (a
        verified, reviewed or corroborated link either way), each row marked ``via`` its
        type; at most ``MAX_NEIGHBOURS`` records."""
        ids = [o for o, _b in objects]
        marks = ",".join("?" * len(ids))
        found = self.conn.execute(
            f"SELECT to_object FROM link_rows WHERE from_object IN ({marks}) AND "
            f"superseded_at IS NULL AND tier IN {FIRM} UNION SELECT from_object FROM link_rows "
            f"WHERE to_object IN ({marks}) AND superseded_at IS NULL AND tier IN {FIRM}",
            (*ids, *ids)).fetchall()
        wanted = set(types)
        linked = sorted({o for (o,) in found if o.split(":", 1)[0] in wanted})
        if not linked or len(linked) > MAX_NEIGHBOURS:
            return []
        out = []
        for oid in linked:
            kind = oid.split(":", 1)[0]
            out += [dict(r, via=kind) for r in self.values([(oid, "")], kind)]
        return out

    def binding_of(self, object_id: str) -> str:
        found = self.conn.execute("SELECT binding_id FROM objects WHERE object_id = ?",
                                  (object_id,)).fetchone()
        return found[0] if found else ""

    def linked_to(self, party: str, record_type: str, bindings: set[str]) -> list[str]:
        """The records of ``record_type`` (of ``bindings``) firmly linked to ``party``."""
        found = self.conn.execute(
            "SELECT l.from_object FROM link_rows l JOIN objects o ON o.object_id = "
            f"l.from_object WHERE l.to_object = ? AND l.superseded_at IS NULL AND l.tier IN "
            f"{FIRM} AND o.object_type = ? UNION SELECT l.to_object FROM link_rows l JOIN "
            "objects o ON o.object_id = l.to_object WHERE l.from_object = ? AND "
            f"l.superseded_at IS NULL AND l.tier IN {FIRM} AND o.object_type = ?",
            (party, record_type, party, record_type)).fetchall()
        ids = sorted({o for (o,) in found})
        if len(ids) > MAX_SIBLINGS:
            return []
        out = []
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            out += [o for (o,) in self.conn.execute(
                f"SELECT object_id FROM objects WHERE object_id IN "
                f"({','.join('?' * len(chunk))}) AND binding_id IN "
                f"({','.join('?' * len(bindings))})", (*chunk, *sorted(bindings)))]
        return out

    def days(self, ids: Sequence[str], props: Sequence[str]) -> dict[str, str]:
        """Each record's day (the first of ``props`` it holds)."""
        out: dict[str, str] = {}
        if not props:
            return out
        for start in range(0, len(ids), 500):
            chunk = list(ids[start:start + 500])
            for oid, value in self.conn.execute(
                    "SELECT object_id, value FROM object_values WHERE object_id IN "
                    f"({','.join('?' * len(chunk))}) AND property IN "
                    f"({','.join('?' * len(props))}) AND superseded_at IS NULL",
                    (*chunk, *props)):
                if value and oid not in out:
                    out[oid] = str(value)[:10]
        return out

    def binding_rows(self, binding: str, props: Sequence[str]) -> list[tuple[str, str, str]]:
        """(object id, property, value) of ``props`` on every current record of one binding
        (cached): what a vague description must pick out one record among."""
        if binding not in self._binding:
            self._binding[binding] = self.conn.execute(
                "SELECT o.object_id, v.property, v.value FROM objects o JOIN object_values v "
                "ON v.object_id = o.object_id WHERE o.binding_id = ? AND "
                f"v.property IN ({','.join('?' * len(props))}) AND v.superseded_at IS NULL",
                (binding, *props)).fetchall()
        return self._binding[binding]


COUNTERPARTY = re.compile(r"^(?:vendor|supplier|customer)_?(?:name|display_?name)$")
RECORD_DAY = re.compile(r"^(?:order_date|document_date|posting_date|invoice_date|"
                        r"date_generated|received_date|receipt_date)$")


def vague_phrase(projection: Projection, rows: Sequence[Mapping[str, Any]],
                 objects: Sequence[tuple[str, str]], object_type: str, as_of: date
                 ) -> str | None:
    """"that <counterparty word> <type word> from <month>" when exactly one record of the
    record's own binding has a counterparty with that word and a day in that month."""
    word = bm.TYPE_WORDS.get(object_type)
    if not word:
        return None
    for object_id, binding in objects:
        mine = [r for r in rows if r["object_id"] == object_id]
        party = next((str(r["value"]) for r in mine if COUNTERPARTY.match(r["property"])
                      and r["value"]), None)
        day = next((str(r["value"])[:10] for r in mine if RECORD_DAY.match(r["property"])
                    and re.match(r"\d{4}-\d{2}-\d{2}", str(r["value"] or ""))), None)
        if not party or not day:
            continue
        props = sorted({r["property"] for r in mine if COUNTERPARTY.match(r["property"])
                        or RECORD_DAY.match(r["property"])})
        every = projection.binding_rows(binding, props)
        parties: dict[str, str] = {}
        days: dict[str, str] = {}
        for oid, prop, value in every:
            if COUNTERPARTY.match(prop) and value:
                parties[oid] = str(value)
            elif value:
                days[oid] = str(value)[:10]
        short = bm.vague_name(party, set(parties.values()))
        if not short:
            continue
        month = day[:7]
        same = [oid for oid, name in parties.items()
                if re.search(rf"\b{re.escape(short)}\b", name, re.I)
                and days.get(oid, "")[:7] == month]
        if same == [object_id]:
            when = date.fromisoformat(day)
            return f"that {short} {word} from {bm.month_phrase(when, as_of)}"
    return _linked_vague(projection, rows, objects, word, as_of)


PARTY_TYPES = ("Customer", "Supplier")
PARTY_NAME = re.compile(r"^(?:name|display_name|company_name|customer_name|vendor_name)$")


def _linked_vague(projection: Projection, rows: Sequence[Mapping[str, Any]],
                  objects: Sequence[tuple[str, str]], word: str, as_of: date) -> str | None:
    """The vague phrase through the record's one linked customer or supplier (a record
    whose counterparty is a link, not a column): its name's distinctive word among every
    party of that binding, and no other record of the record's own binding linked to that
    party with a day in the same month."""
    day = next((str(r["value"])[:10] for r in rows if not r.get("via")
                and RECORD_DAY.match(r["property"])
                and re.match(r"\d{4}-\d{2}-\d{2}", str(r["value"] or ""))), None)
    if not day:
        return None
    parties = [r for r in projection.neighbours(objects, PARTY_TYPES)
               if PARTY_NAME.match(r["property"]) and r["value"]]
    if len({r["object_id"] for r in parties}) != 1:
        return None
    party_id = parties[0]["object_id"]
    names = {str(r["value"]).strip() for r in parties}
    if len({n.casefold() for n in names}) != 1:
        return None
    binding = projection.binding_of(party_id)
    others = {str(v) for _o, _p, v in projection.binding_rows(
        binding, sorted({r["property"] for r in parties})) if v}
    short = bm.vague_name(next(iter(names)), others)
    if not short:
        return None
    mine = {o for o, _b in objects}
    record_type = next(iter(mine)).split(":", 1)[0]
    siblings = projection.linked_to(party_id, record_type, {b for _o, b in objects})
    days = projection.days(siblings, sorted({r["property"] for r in rows
                                             if RECORD_DAY.match(r["property"])}))
    same = {o for o, d in days.items() if d[:7] == day[:7]}
    if not same or not same <= mine:
        return None
    return f"that {short} {word} from {bm.month_phrase(date.fromisoformat(day), as_of)}"


# --------------------------------------------------------------------------- build

def _day(at: Any) -> str:
    return str(at or "")[:10]


def candidates(entries: Sequence[Mapping[str, Any]], projection: Projection, as_of: date
               ) -> tuple[list[dict[str, Any]], Counter]:
    """Each mined question the oracle settles: the record, the ask, the answer and the
    standalone question; ``Counter``: why the others were dropped (counts only)."""
    dropped: Counter = Counter()
    out = []
    resolved: dict[tuple[str, str], Any] = {}
    for entry in entries:
        question = entry["question"]
        if not bm.topical(question):
            dropped["not_a_records_question"] += 1
            continue
        codes = bm.codes_in(question)
        from_subject = None
        if not codes:
            # the thread's subject names the record a question with no code is about
            codes = bm.codes_in(entry.get("subject") or "")
            pronoun = bm.PRONOUN.search(question)
            from_subject = pronoun.group(0).casefold() if pronoun else ""
            if not pronoun and len(question.split()) > SUBJECT_WORDS:
                dropped["no_single_record"] += 1   # a long line is about more than its thread
                continue
        hits = {}
        for code in codes:
            cache_key = (code.text.upper(), code.word)
            if cache_key not in resolved:
                resolved[cache_key] = projection.resolve(code)
            if resolved[cache_key] is not None:
                hits[resolved[cache_key][1]] = (code, resolved[cache_key])
        if len(hits) != 1:
            dropped["no_single_record" if not hits else "several_records"] += 1
            continue
        code, (object_type, stored, objects) = next(iter(hits.values()))
        noun = (from_subject or "").rsplit(" ", 1)[-1]
        if noun in bm.WORD_TYPES and object_type not in bm.WORD_TYPES[noun]:
            dropped["pronoun_names_another_kind"] += 1   # "this order" vs an item code
            continue
        ask = bm.ask_of(question)
        if ask is None:
            dropped["ask_unknown"] += 1
            continue
        rows = projection.values(objects, object_type)
        if ask.via:
            rows += projection.neighbours(objects, ask.via)
        found = bm.answer(ask, rows, as_of, question)
        if found is None:
            dropped["oracle_unsettled"] += 1
            continue
        if any(scoring_echo(question, f) for f in found.facts if f["kind"] != "absent"):
            dropped["answer_in_question"] += 1
            continue
        written = bm.written_code(object_type, stored)
        asked = bm.normalize(question, bm.Code("", "", 0, 0) if from_subject is not None else code,
                             written, as_of)
        if asked is None:
            dropped["not_standalone"] += 1
            continue
        out.append({"entry": entry, "ask": ask.name, "answer": found, "question": asked,
                    "written": written, "stored": stored, "object_type": object_type,
                    "objects": objects, "rows": rows})
    # no one ask fills the family: each keeps at most ``MAX_PER_ASK`` bases, drawn by hash
    out.sort(key=lambda c: bm._hash(c["ask"], c["question"]))
    taken: Counter = Counter()
    kept = []
    for found in out:
        taken[found["ask"]] += 1
        if taken[found["ask"]] > MAX_PER_ASK:
            dropped["ask_share_capped"] += 1
            continue
        kept.append(found)
    return kept, dropped


def perturbed(base: Mapping[str, Any], projection: Projection, as_of: date
              ) -> list[tuple[str, str]]:
    """(op, question) of up to ``PERTURBED_PER_BASE`` perturbations of a base question,
    the ops tried in an order drawn by SHA-256 of the question."""
    question, written, stored = base["question"], base["written"], base["stored"]
    seed = bm._hash(question)
    ops = sorted(bm.PERTURBATIONS, key=lambda op: bm._hash(seed, op))
    out = []
    for op in ops:
        made = None
        if op == "typo":
            # the slip goes in the asked words, never the date anchor or the code
            head, comma, body = question.partition(", ")
            made = head + comma + bm.typo(body, seed, keep=written) if comma else None
        elif op == "recode":
            other = bm.recode(stored, seed)
            made = question.replace(stored, other, 1) if other and stored in question \
                else None
        elif op == "relative":
            made = bm.relative(question, as_of, seed)
        elif op == "vague":
            phrase = vague_phrase(projection, base["rows"], base["objects"],
                                  base["object_type"], as_of)
            made = bm.vague(question, written, phrase) if phrase else None
        if made and made != question:
            out.append((op, made))
        if len(out) >= PERTURBED_PER_BASE:
            break
    return out


def build(entries: Sequence[Mapping[str, Any]], projection: Projection, *, as_of: date,
          generation: str, now: str, dev_share: float
          ) -> tuple[list[dict], list[dict], dict[str, Any]]:
    bases, dropped = candidates(entries, projection, as_of)
    digest = hashlib.sha256("\x1f".join(sorted(b["question"] for b in bases)).encode()
                            ).hexdigest()
    items: list[dict] = []
    keys: list[dict] = []
    systems: list[str] = []
    for n, base in enumerate(bases):
        entry = base["entry"]
        source = {"system": entry["system"], "handle": entry["handle"],
                  "day": _day(entry.get("at"))}
        variants = [("standard", base["question"])] + perturbed(base, projection, as_of)
        for op, question in variants:
            item, key = bm.item_and_key(
                f"blind#{n}#{op}", question, base["answer"], root=base["stored"],
                object_type=base["object_type"], now=now, as_of=as_of, source=source,
                variant=op, generations={"projection": generation}, digest=digest)
            key["card"] = f"blind-{base['ask']}-{op}"
            items.append(item)
            keys.append(key)
            systems.append(entry["system"])
    shadows = [lo._shadow(i, k) for i, k in zip(items, keys)]
    visible = [item for root in runner.VISIBLE_ITEM_DIRS if root.is_dir()
               for item in runner.load_items(root, split="dev", admit=False)]
    seen_dev = {d for v in visible + runner.visible_cards() for d in partition.key_digests(v)}
    forced = partition.related(shadows, seen_dev)
    dealt = bm.deal(partition.groups(shadows), dev_share, digest, forced)
    counters = {"dev": 0, "held_out": 0}
    for index, (item, key) in enumerate(zip(items, keys)):
        split = dealt[index]
        counters[split] += 1
        item["split"] = split
        item["id"] = f"{'dev' if split == 'dev' else 'ho'}-{bm.FAMILY}-{counters[split]:04d}"
        key["item_id"] = item["id"]
    stats = {"mined": len(entries), "bases": len(bases), "items": len(items),
             "dev": counters["dev"], "held_out": counters["held_out"],
             "dropped": dict(sorted(dropped.items())),
             "by_system": dict(Counter(systems)),
             "by_ask": dict(Counter(k["card"].split("-")[1] for k in keys)),
             "by_variant": dict(Counter(k["card"].rsplit("-", 1)[1] for k in keys)),
             "by_answer": dict(Counter("not_known" if k["facts"][0]["kind"] == "absent"
                                       else "value" for k in keys)),
             "by_record_type": dict(Counter(i["root"]["object_type"] for i in items))}
    return items, keys, stats


def main(argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build")
    b.add_argument("--dev-dir", type=Path, required=True)
    b.add_argument("--state", type=Path)
    b.add_argument("--dev-share", type=float, default=DEFAULT_DEV_SHARE)
    b.add_argument("--show-dev", action="store_true")
    args = parser.parse_args(argv)
    environ = os.environ if environ is None else environ
    try:
        if lo._inside_git(args.dev_dir):
            raise Refused("dev_dir_inside_a_git_work_tree")
        named = args.state or environ.get(lo.real_state_env())
        if not named or not Path(named).is_dir():
            raise Refused("state_missing")
        state = Path(named)
        held = lo.held_out_dir(args.dev_dir)   # refuses outside the credentialed env
        store, generation, as_of = current_generation(state)
        entries = mined(state, environ)
        projection = Projection(store)
        try:
            now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            items, keys, stats = build(entries, projection, as_of=as_of,
                                       generation=generation, now=now,
                                       dev_share=args.dev_share)
        finally:
            projection.close()
        for split, directory in (("dev", args.dev_dir), ("held_out", held)):
            pairs = [(i, k) for i, k in zip(items, keys) if i["split"] == split]
            lo.write_split(directory, [i for i, _ in pairs], [k for _, k in pairs])
        for item in items:
            errors = runner.validate_item(item)
            if errors:
                raise Refused(f"item_invalid:{errors[0]}")
        stats.update(generation=generation, as_of=as_of.isoformat())
        if args.show_dev:
            stats["dev_items"] = [{"id": i["id"], "question": i["question"]["text"],
                                   "ask": k["card"], "answer": k["facts"][0]["kind"]}
                                  for i, k in zip(items, keys) if i["split"] == "dev"]
    except (Refused, lo.MailRefused, runner.RunnerError, OSError, ValueError,
            sqlite3.Error) as exc:
        print(json.dumps({"error": type(exc).__name__, "code": str(exc)[:200]}),
              file=sys.stderr)
        return 1
    print(json.dumps(stats, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
