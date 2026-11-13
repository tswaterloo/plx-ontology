"""SaaS notification mail we already hold, read into structured notice records.

Carrier tracking notices (UPS, FedEx, DHL), payment and remittance notices (APS Payments,
Intuit, Billtrust), Zendesk tickets, Netstock replenishment notices, EDI notices
(EDISoft, CommerceHub) and Concur expense notices arrive as mail. This module reads them
with declared parsers (``notice_mail_families.json``: one entry per sender family, its
kinds and its field patterns), never per-message code, into one record per message:

* ``family``, ``record_type``, ``kind`` and the parsed ``fields`` (tracking numbers,
  dates as ISO days, money as ``Decimal`` text with its currency, codes and references);
* ``links``: each linked field's values with the identifier kind they resolve through
  (``plx.find``'s one grammar: tracking number, invoice number, customer PO, item code,
  a free reference), so a shipment, order, PO, invoice, customer or item is reached by
  reference, never by a rule naming one;
* ``citation`` and ``source_acl``: the message's own, so a notice is visible to exactly
  the readers its message is (provenance kept, never widened);
* ``markings``: what a reader must also hold. Payment notices are ``finance``; Concur
  notices ``finance`` and ``pii``: served only to a reader holding both.

People exclusion: a message any excluded person sent or received yields no record; the
exclusion must be given (``people``), else :func:`parse` refuses (fail closed).

Derived: :func:`delivery_late` (an alert per tracking number delivered after, or not
delivered by, the date its carrier gave) and :func:`payment_received` (a fact per invoice
number a payment notice names, finance-marked).

``python3 -m integrations.notice_mail measure --mail-state DIR --as-of ISO`` counts each
family over a read-only mail store and validates 15 random parsed records per family
(each field re-checked by an independent validator); it prints counts only, never a body,
subject or value, and refuses with under 10 GB free. Stdlib only.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import html
import json
import os
from pathlib import Path
import random
import re
import shutil
import sqlite3
import sys
from typing import Any, Callable, Iterable, Mapping, Sequence

FAMILIES_FILE = Path(__file__).with_name("notice_mail_families.json")
SCHEMA = "plx-notice-mail-families/v1"
FIELD_TYPES = ("tracking_ups", "tracking_fedex", "tracking_dhl", "date", "money", "code",
               "text", "reference")
MARKINGS = frozenset({"finance", "pii"})
MAX_TEXT = 200_000          # characters of a body read
MAX_VALUES = 20             # values kept per many-field
MIN_FREE_BYTES = 10 * 2**30
LATE_GRACE = timedelta(days=1)


class NoticeError(ValueError):
    """A families document or input refused, with a code."""


@dataclass(frozen=True)
class Family:
    id: str
    record_type: str
    senders: tuple[re.Pattern, ...]
    markings: frozenset[str]
    kinds: tuple[tuple[str, tuple[re.Pattern, ...]], ...]
    kind_types: Mapping[str, str]
    fields: Mapping[str, tuple[str, bool, tuple[re.Pattern, ...]]]
    required: tuple[str, ...]
    links: Mapping[str, str]
    carrier: str | None = None
    skip_replies: bool = False


@dataclass
class Notice:
    family: str
    record_type: str
    kind: str
    fields: dict[str, Any]
    links: list[dict[str, str]]
    markings: tuple[str, ...]
    citation: str
    received_at: str | None
    source_acl: Any = None
    carrier: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"family": self.family, "record_type": self.record_type, "kind": self.kind,
                "carrier": self.carrier, "fields": self.fields, "links": self.links,
                "markings": list(self.markings), "citation": self.citation,
                "received_at": self.received_at}


def _compile(patterns: Any, where: str) -> tuple[re.Pattern, ...]:
    if not isinstance(patterns, list) or not patterns \
            or not all(isinstance(p, str) and p for p in patterns):
        raise NoticeError(f"notice_family_invalid:{where}")
    try:
        return tuple(re.compile(p, re.IGNORECASE | re.MULTILINE) for p in patterns)
    except re.error:
        raise NoticeError(f"notice_pattern_invalid:{where}") from None


def load_families(document: Any = None) -> tuple[tuple[Family, ...], tuple[str, ...]]:
    """(families, date formats) of the declared document; anything malformed refuses
    (an unknown field type or marking, a pattern without exactly one group, a reference
    ``@family`` that does not exist)."""
    if document is None:
        document = json.loads(FAMILIES_FILE.read_text("utf-8"))
    if not isinstance(document, Mapping) or document.get("schema") != SCHEMA \
            or not isinstance(document.get("families"), list):
        raise NoticeError("notice_families_invalid")
    raw = {f.get("id"): f for f in document["families"] if isinstance(f, Mapping)}
    if len(raw) != len(document["families"]) or not all(isinstance(k, str) for k in raw):
        raise NoticeError("notice_families_invalid")

    def resolve(value: Any, key: str, sub: str | None = None) -> Any:
        if isinstance(value, str) and value.startswith("@"):
            other = raw.get(value[1:])
            if other is None:
                raise NoticeError(f"notice_reference_unknown:{value}")
            target = other.get(key)
            target = target.get(sub) if sub is not None and isinstance(target, Mapping) \
                else target
            if isinstance(target, str):
                raise NoticeError(f"notice_reference_chained:{value}")
            return target
        return value

    out = []
    for fid, entry in raw.items():
        markings = entry.get("markings", [])
        if not isinstance(markings, list) or set(markings) - MARKINGS:
            raise NoticeError(f"notice_marking_unknown:{fid}")
        kinds = resolve(entry.get("kinds"), "kinds")
        if not isinstance(kinds, list) or not kinds:
            raise NoticeError(f"notice_family_invalid:{fid}.kinds")
        fields = {}
        for name, spec in (entry.get("fields") or {}).items():
            spec = resolve(spec, "fields", name)
            if not isinstance(spec, Mapping) or spec.get("type") not in FIELD_TYPES:
                raise NoticeError(f"notice_field_invalid:{fid}.{name}")
            patterns = _compile(spec.get("patterns"), f"{fid}.{name}")
            if any(p.groups != 1 for p in patterns):
                raise NoticeError(f"notice_pattern_groups:{fid}.{name}")
            fields[name] = (spec["type"], bool(spec.get("many")), patterns)
        required = tuple(entry.get("required") or ())
        links = dict(entry.get("links") or {})
        if set(required) - set(fields) or set(links) - set(fields):
            raise NoticeError(f"notice_family_invalid:{fid}.required")
        out.append(Family(fid, str(entry.get("record_type")), _compile(entry.get("senders"),
                          f"{fid}.senders"), frozenset(markings),
                          tuple((str(k.get("kind")), _compile(k.get("any"), f"{fid}.kinds"))
                                for k in kinds),
                          {str(k["kind"]): str(k["record_type"]) for k in kinds
                           if isinstance(k.get("record_type"), str)},
                          fields, required, links,
                          entry.get("carrier"), bool(entry.get("skip_replies"))))
    formats = document.get("date_formats") or []
    if not isinstance(formats, list) or not all(isinstance(f, str) for f in formats):
        raise NoticeError("notice_families_invalid")
    return tuple(out), tuple(formats)


def sender_domain(address: Any) -> str:
    text = str(address or "").strip().lower().rstrip(">")
    return text.rsplit("@", 1)[-1] if "@" in text else ""


def family_of(families: Sequence[Family], address: Any) -> Family | None:
    domain = sender_domain(address)
    if not domain:
        return None
    return next((f for f in families if any(p.search(domain) for p in f.senders)), None)


_TAG = re.compile(r"<(script|style)\b.*?</\1>|<br\s*/?>|</(p|div|tr|li|h\d)>|<[^>]+>",
                  re.IGNORECASE | re.DOTALL)


def plain_text(body: str | None) -> str:
    """Mail body (HTML or text) as text lines."""
    if not body:
        return ""
    text = _TAG.sub(lambda m: "\n" if m.group(0)[:2].lower() in ("<b", "</") else " ",
                    body[:MAX_TEXT])
    text = html.unescape(text).replace("\xa0", " ")
    return "\n".join(" ".join(line.split()) for line in text.splitlines() if line.strip())


def _date(value: str, formats: Sequence[str]) -> str | None:
    value = re.sub(r"\s+", " ", value.strip().rstrip(".,"))
    for fmt in formats:
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _money(value: str) -> dict[str, str] | None:
    currency = "CAD" if re.search(r"CAD|C\$", value) else "USD" if re.search(
        r"USD|US\$", value) else None
    number = re.sub(r"[^\d.\-]", "", value)
    try:
        amount = Decimal(number)
    except InvalidOperation:
        return None
    return {"amount": str(amount.quantize(Decimal("0.01"))), "currency": currency or "unstated"}


def _value(kind: str, raw: str, formats: Sequence[str]) -> Any:
    raw = raw.strip()
    if kind == "date":
        return _date(raw, formats)
    if kind == "money":
        return _money(raw)
    if kind.startswith("tracking_"):
        return raw.upper() if valid_tracking(kind, raw.upper()) else None
    if kind in ("code", "reference"):
        return raw.upper().strip(".-/")
    return raw.strip(" .,;")


def ups_check_digit_ok(number: str) -> bool:
    """UPS 1Z numbers: the last character is a mod-10 check over the 15 before it."""
    if not re.fullmatch(r"1Z[0-9A-Z]{16}", number):
        return False
    total = 0
    for i, ch in enumerate(number[2:17]):
        n = int(ch) if ch.isdigit() else (ord(ch) - 63) % 10
        total += n * (2 if i % 2 else 1)
    return (10 - total % 10) % 10 == int(number[17]) if number[17].isdigit() else False


def valid_tracking(kind: str, number: str) -> bool:
    if kind == "tracking_ups":
        return ups_check_digit_ok(number)
    if kind == "tracking_fedex":
        return bool(re.fullmatch(r"\d{12}|\d{15}|\d{20}|\d{22}", number)) \
            and len(set(number)) > 2
    if kind == "tracking_dhl":
        return bool(re.fullmatch(r"\d{10}|JJD\d{10,20}|JVGL\d{10,20}", number)) \
            and len(set(number)) > 2
    return False


_REPLY = re.compile(r"^\s*(re|fw|fwd|aw|tr)\s*:", re.IGNORECASE)


def parse(families: Sequence[Family], formats: Sequence[str], message: Mapping[str, Any],
          body: str | None, *, people: Any) -> Notice | None:
    """One message as a notice record, or None (no family, nothing required found, or a
    message an excluded person is party to). ``message``: ``from``, ``to``, ``cc``,
    ``subject``, ``received_at``, ``citation``, ``source_acl``. ``people``: the exclusion
    (``excluded(identities) -> bool``); None refuses (``people_exclusion_unavailable``)."""
    if people is None or not callable(getattr(people, "excluded", None)):
        raise NoticeError("people_exclusion_unavailable")
    family = family_of(families, message.get("from"))
    if family is None:
        return None
    parties = [message.get("from"), *(message.get("to") or []), *(message.get("cc") or [])]
    identities = [str(p).strip().lower() for p in parties if p]
    if people.excluded(identities):
        return None
    subject = str(message.get("subject") or "")
    if family.skip_replies and _REPLY.match(subject):
        return None   # a person replying or forwarding: correspondence, not a notice
    text = subject + "\n" + plain_text(body)
    kind = next((name for name, patterns in family.kinds
                 if any(p.search(subject) for p in patterns)), None) \
        or next((name for name, patterns in family.kinds
                 if any(p.search(text) for p in patterns)), None)
    if kind is None:
        return None   # no declared kind: correspondence from the sender, not a notice
    fields: dict[str, Any] = {}
    for name, (ftype, many, patterns) in family.fields.items():
        found: list[Any] = []
        for pattern in patterns:
            for match in pattern.finditer(text):
                value = _value(ftype, match.group(1), formats)
                if value is not None and value not in found:
                    found.append(value)
                if found and not many:
                    break
            if found:   # the first pattern that matches wins, for one value or many
                break
        if found:
            fields[name] = found[:MAX_VALUES] if many else found[0]
    if any(name not in fields for name in family.required):
        return None
    links = [{"field": name, "identifier_kind": kind_, "value": v if isinstance(v, str)
              else json.dumps(v, sort_keys=True)}
             for name, kind_ in family.links.items()
             for v in (fields.get(name) if isinstance(fields.get(name), list)
                       else [fields[name]] if name in fields else [])]
    return Notice(family.id, family.kind_types.get(kind, family.record_type), kind, fields,
                  links,
                  tuple(sorted(family.markings)), str(message.get("citation") or ""),
                  message.get("received_at"), message.get("source_acl"), family.carrier)


def visible(notice: Notice, reader_markings: Iterable[str],
            may_read: Callable[[Any], bool]) -> bool:
    """A reader sees a notice only when it may read the message it came from
    (``may_read(source_acl)``) and holds every marking the notice carries."""
    return set(notice.markings) <= set(reader_markings) and bool(may_read(notice.source_acl))


def for_reader(notices: Iterable[Notice], reader_markings: Iterable[str],
               may_read: Callable[[Any], bool]) -> list[Notice]:
    held = frozenset(reader_markings)
    return [n for n in notices if visible(n, held, may_read)]


# ---------------------------------------------------------------- derived

def delivery_late(notices: Iterable[Notice], today: date) -> list[dict[str, Any]]:
    """Per tracking number of the carrier notices: late when delivered after the latest
    date its carrier gave, or not delivered a day past it. Each alert cites every notice
    it read and carries their references (orders, POs) for linking."""
    by_number: dict[str, list[Notice]] = {}
    for notice in notices:
        if notice.record_type != "CarrierNotice":
            continue
        for number in notice.fields.get("tracking_numbers") or ():
            by_number.setdefault(number, []).append(notice)
    alerts = []
    for number, group in sorted(by_number.items()):
        group.sort(key=lambda n: n.received_at or "")
        promised = [n.fields["scheduled_delivery"] for n in group
                    if n.fields.get("scheduled_delivery")]
        delivered = [n.fields.get("delivered_on") or (n.received_at or "")[:10]
                     for n in group if n.kind == "delivered"]
        if not promised:
            continue
        expected = date.fromisoformat(promised[-1])
        if delivered:
            on = date.fromisoformat(min(delivered))
            if on <= expected:
                continue
            state, days = "delivered_late", (on - expected).days
        elif today > expected + LATE_GRACE:
            state, days = "not_delivered", (today - expected).days
        else:
            continue
        alerts.append({"alert": "delivery_late", "tracking_number": number,
                       "carrier": group[0].carrier, "state": state, "days_late": days,
                       "expected": expected.isoformat(),
                       "references": sorted({r for n in group
                                             for r in n.fields.get("references") or ()}),
                       "citations": [n.citation for n in group],
                       "markings": sorted({m for n in group for m in n.markings})})
    return alerts


def payment_received(notices: Iterable[Notice]) -> list[dict[str, Any]]:
    """A fact per invoice number a payment-received notice names: the amount (Decimal
    text, its currency), when, the citation; finance-marked like its notice. A notice
    naming several invoices gives each the notice's total, flagged ``shared_total``."""
    facts = []
    for notice in notices:
        if notice.record_type != "PaymentNotice" or notice.kind != "payment_received":
            continue
        invoices = notice.fields.get("invoice_numbers") or []
        amount = notice.fields.get("amount")
        for invoice in invoices:
            facts.append({"fact": "payment_received", "invoice_number": invoice,
                          "amount": amount, "shared_total": len(invoices) > 1,
                          "paid_at": notice.received_at, "citation": notice.citation,
                          "payer": notice.fields.get("payer"),
                          "markings": sorted(set(notice.markings) | {"finance"})})
    return facts


# ---------------------------------------------------------------- measurement

def validate(notice: Notice, text: str) -> bool:
    return failed_field(notice, text) is None


def failed_field(notice: Notice, text: str) -> str | None:
    """Every parsed value re-checked independently of its pattern: it occurs in the
    message text, a tracking number passes its carrier's format (UPS its check digit), a
    date is a real day within two years of the message, money is a non-negative
    Decimal with at most two places."""
    received = (notice.received_at or "")[:10]
    for name, value in notice.fields.items():
        for v in value if isinstance(value, list) else [value]:
            if isinstance(v, dict):
                amount = Decimal(v["amount"])
                if amount < 0 or amount.as_tuple().exponent < -2 \
                        or v["amount"].split(".")[0].lstrip("-") not in text.replace(",", ""):
                    return name
            elif name in ("scheduled_delivery", "delivered_on", "shipped_on"):
                try:
                    day = date.fromisoformat(v)
                    if received and abs((day - date.fromisoformat(received)).days) > 730:
                        return name
                except ValueError:
                    return name
            elif name == "tracking_numbers":
                kind = {"UPS": "tracking_ups", "FedEx": "tracking_fedex",
                        "DHL": "tracking_dhl"}[notice.carrier or "UPS"]
                if not valid_tracking(kind, v) or v not in text.upper():
                    return name
            elif str(v).upper() not in text.upper():
                return name
    return None


def measure(mail_state: Path, as_of: str, *, sample: int = 15, seed: int = 11,
            people: Any, only: Sequence[str] = ()) -> dict[str, Any]:
    """Counts per family over the mail store's current messages recorded up to
    ``as_of`` (read-only), and ``sample`` random parsed records per family validated."""
    if shutil.disk_usage(mail_state).free < MIN_FREE_BYTES:
        raise NoticeError("disk_below_10_gb")
    from integrations.capture_mail import MailCaptureStore
    families, formats = load_families()
    store = MailCaptureStore.open_read_only(mail_state)
    db = sqlite3.connect(f"file:{Path(mail_state) / 'mail-capture' / 'capture.db'}?mode=ro",
                         uri=True, timeout=5)
    counts = {f.id: {"messages": 0, "parsed": 0, "with_body": 0} for f in families}
    pools: dict[str, list[tuple[int, str]]] = {f.id: [] for f in families}
    rnd = random.Random(seed)
    from integrations.capture_mail import undecided_free_sql
    rows = db.execute("SELECT version_id, record, body_sha256 FROM messages WHERE "
                      f"superseded_at IS NULL AND recorded_at <= ? AND {undecided_free_sql(db)}",
                      (as_of,))
    for version_id, record, digest in rows:
        try:
            message = json.loads(record)
        except ValueError:
            continue
        family = family_of(families, message.get("from"))
        if family is None:
            continue
        counts[family.id]["messages"] += 1
        if digest:
            counts[family.id]["with_body"] += 1
            pool, seen = pools[family.id], counts[family.id]["with_body"]
            if len(pool) < 400:
                pool.append((version_id, digest))
            elif rnd.random() < 400 / seen:
                pool[rnd.randrange(400)] = (version_id, digest)
    result: dict[str, Any] = {}
    for family in families:
        parsed, checked, good, kinds, failures = 0, 0, 0, {}, {}
        if only and family.id not in only:
            continue
        for version_id, digest in pools[family.id]:
            message = json.loads(db.execute("SELECT record FROM messages WHERE version_id=?",
                                            (version_id,)).fetchone()[0])
            try:
                body = store.body_text({"body_sha256": digest})
            except Exception:  # noqa: BLE001 - a dropped blob is a message without a body
                body = None
            notice = parse(families, formats, message, body, people=people)
            if notice is None:
                continue
            parsed += 1
            kinds[notice.kind] = kinds.get(notice.kind, 0) + 1
            if checked < sample:
                checked += 1
                text = (str(message.get("subject") or "") + "\n" + plain_text(body))
                bad = failed_field(notice, text)
                good += bad is None
                if bad is not None:
                    failures[bad] = failures.get(bad, 0) + 1
        pool_n = len(pools[family.id])
        counts[family.id].update(
            pool=pool_n, parsed_in_pool=parsed,
            parsed_estimate=round(counts[family.id]["with_body"] * parsed / pool_n)
            if pool_n else 0, kinds_in_pool=kinds, sampled=checked, validated=good,
            precision=round(good / checked, 3) if checked else None,
            failed_fields=failures)
        result[family.id] = counts[family.id]
    db.close()
    return {"as_of": as_of, "families": result}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Notice mail: measure parsers (counts only).")
    sub = parser.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("measure")
    m.add_argument("--mail-state", required=True, type=Path,
                   help="state dir holding mail-capture/ (opened read-only)")
    m.add_argument("--as-of", required=True)
    m.add_argument("--sample", type=int, default=15)
    m.add_argument("--family", action="append", default=[])
    args = parser.parse_args(argv)
    from integrations.people_exclusion import PeopleExclusion
    try:
        people = PeopleExclusion.from_env(os.environ)
    except Exception:  # noqa: BLE001 - an unloadable exclusion refuses the run
        print(json.dumps({"error": "people_exclusion_unavailable"}))
        return 2
    try:
        report = measure(args.mail_state, args.as_of, sample=args.sample, only=args.family,
                         people=_AddressExclusion(people, mailbox_users(args.mail_state)))
    except NoticeError as exc:
        print(json.dumps({"error": str(exc)}))
        return 2
    print(json.dumps(report, indent=1, sort_keys=True))
    return 0


class _AddressExclusion:
    """``PeopleExclusion`` over the addresses a message names, decided offline from the
    mail store's captured mailboxes (object id, UPN, addresses). An address in a tenant
    domain the directory does not map stays undecided: the message is withheld (fail
    closed); an address outside every tenant domain is no tenant person's."""

    def __init__(self, people: Any, users: Iterable[tuple[Any, Any, Iterable[Any]]] = ()
                 ) -> None:
        self.people = people
        if users:
            people.attach_directory(users)
        self.domains = people.known_domains() if users else frozenset()
        self.undecided = 0

    def excluded(self, identities: Sequence[str]) -> bool:
        tenant = [i for i in identities if not self.domains
                  or i.rsplit("@", 1)[-1] in self.domains or "@" not in i]
        try:
            return bool(self.people.excluded(tenant))
        except Exception:  # noqa: BLE001 - an address it cannot decide withholds the message
            self.undecided += 1
            return True


def mailbox_users(mail_state: Path) -> list[tuple[str, str, list[str]]]:
    """(object id, UPN, addresses) of every captured mailbox, read-only."""
    db = sqlite3.connect(f"file:{Path(mail_state) / 'mail-capture' / 'capture.db'}?mode=ro",
                         uri=True, timeout=5)
    try:
        out = []
        for oid, upn, identities in db.execute(
                "SELECT mailbox_id, upn, identities FROM mailboxes"):
            try:
                more = json.loads(identities) if identities else []
            except ValueError:
                more = []
            out.append((oid, upn, [m for m in more if isinstance(m, str)]
                        if isinstance(more, list) else []))
        return out
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
